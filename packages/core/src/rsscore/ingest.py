"""Orquestación de la ingesta: red fuera, base de datos dentro.

El invariante que estructura todo el módulo es la separación entre el trabajo de
red (lento, concurrente, propenso a fallar) y el trabajo de base de datos
(rápido, secuencial, transaccional):

    descargar → parsear → clasificar → [extraer texto completo] → BEGIN … COMMIT

Entre `BEGIN` y `COMMIT` **no hay un solo `await`**. Eso consigue dos cosas a la
vez: que 200 inserciones sean un único fsync en lugar de 200, y que varias
tareas del mismo bucle de eventos no puedan entrelazar sus transacciones sobre
la conexión compartida (SQLite no anida transacciones).

La deduplicación tiene dos niveles con intenciones distintas:

* `guid_hash` dentro del feed: identidad. Si vuelve la misma entrada con otro
  contenido, se *actualiza*; nunca se duplica.
* `content_hash` entre feeds: la misma noticia sindicada por varios medios. No
  se descarta —el usuario quiere ver que Reuters y El País cuentan lo mismo—,
  se anota en el resultado para que la interfaz pueda agruparlas.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

import anyio

from . import repo
from .config import Config, FetchConfig
from .extract import extract_full_text, should_extract
from .fetch import MIN_SCRAPE_INTERVAL, Fetcher, FetchResult, next_interval
from .ids import now_ms
from .models import Entry, Feed
from .parse import ParsedFeed, discover_feed_links, parse_feed
from .scrape import ScrapeError, guess_selectors, parse_source

log = logging.getLogger(__name__)

IngestStatus = Literal["ok", "not_modified", "error", "skipped"]

OnNewEntry = Callable[[sqlite3.Connection, Entry, Feed], None]

# Rutas donde suele vivir un feed que la página no enlaza.
COMMON_FEED_PATHS = (
    "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
    "/index.xml", "/feed.json", "/?feed=rss2", "/blog/feed", "/news/rss",
)

class NoFeedFound(ValueError):
    """No hay feed, pero quizá se pueda raspar la página."""

    def __init__(self, url: str, candidates: list) -> None:
        self.url = url
        self.candidates = candidates
        detalle = (
            f"; se puede raspar con «{candidates[0].config.item_selector}»"
            if candidates
            else ""
        )
        super().__init__(f"no se encontró ningún feed en {url}{detalle}")


@dataclass(slots=True)
class IngestResult:
    """Qué pasó al refrescar un feed. Es lo que consume la interfaz y el log."""

    feed_id: str
    status: IngestStatus = "ok"
    new_entries: list[str] = field(default_factory=list)
    updated: int = 0
    skipped: int = 0
    error: str | None = None
    # entrada nueva → entrada ya existente en otro feed con el mismo contenido
    duplicate_of: dict[str, str] = field(default_factory=dict)
    full_text: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.new_entries) or self.updated > 0


class Ingestor:
    """Refresca feeds y vuelca sus entradas en el archivo."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config | FetchConfig | None = None,
        *,
        fetcher: Fetcher | None = None,
        on_new_entry: OnNewEntry | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = _fetch_cfg(cfg)
        self.fetcher = fetcher or Fetcher(self.cfg)
        self.on_new_entry = on_new_entry

    # -- ciclo de vida ------------------------------------------------------
    async def __aenter__(self) -> Ingestor:
        await self.fetcher.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    # -- refresco de un feed ------------------------------------------------
    async def refresh_feed(self, feed: Feed) -> IngestResult:
        """Descarga, parsea, deduplica e inserta. No lanza: los fallos se devuelven."""
        result = IngestResult(feed_id=feed.id)
        started = now_ms()

        fetched = await self.fetcher.fetch(feed)

        if fetched.status == "not_modified":
            result.status = "not_modified"
            self._record_success(feed, fetched, changed=False, at=started)
            return result

        if fetched.status == "error":
            result.status = "error"
            result.error = fetched.error
            self._record_error(feed, fetched.error or "error desconocido", at=started)
            return result

        try:
            parsed, watch_hash = self._parse_source(feed, fetched)
        except ScrapeError as exc:
            result.status = "error"
            result.error = str(exc)
            self._record_error(feed, str(exc), at=started)
            return result

        if parsed is None:
            reason = "la respuesta no es un feed reconocible"
            result.status = "error"
            result.error = reason
            self._record_error(feed, reason, at=started)
            return result

        try:
            await self._store(feed, parsed, fetched, result, started, watch_hash=watch_hash)
        except Exception as exc:                   # error de base de datos, no de red
            log.exception("fallo al guardar el feed %s", feed.url)
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            self._record_error(feed, result.error, at=started)
        return result

    def _parse_source(
        self, feed: Feed, fetched: FetchResult
    ) -> tuple[ParsedFeed | None, str | None]:
        """Bytes → entradas, según de dónde venga el feed.

        Es el único punto del sistema que sabe que existen webs sin RSS: a
        partir de aquí un artículo raspado es una entrada como cualquier otra.
        """
        base_url = fetched.final_url or feed.url

        if feed.source_kind == "feed":
            parsed = parse_feed(fetched.content or b"", feed.id, base_url=base_url)
            return (parsed if parsed.is_feed else None), None

        import json

        try:
            config = json.loads(feed.source_config_json or "{}")
        except json.JSONDecodeError as exc:
            raise ScrapeError(f"la configuración de la fuente está corrupta: {exc}") from exc

        return parse_source(
            fetched.text(), feed.id, feed.source_kind, config,
            base_url=base_url, previous_hash=feed.watch_hash,
        )

    async def _store(
        self,
        feed: Feed,
        parsed: ParsedFeed,
        fetched: FetchResult,
        result: IngestResult,
        started: int,
        *,
        watch_hash: str | None = None,
    ) -> None:
        # 1) Clasificar contra lo que ya hay (solo lecturas, fuera de transacción).
        to_insert: list[Entry] = []
        to_update: list[tuple[str, Entry]] = []
        for entry in parsed.entries:
            existing_id = repo.entry_exists(self.conn, feed.id, entry.guid_hash)
            if existing_id is None:
                to_insert.append(entry)
                continue
            if self._content_changed(existing_id, entry):
                to_update.append((existing_id, entry))
            else:
                result.skipped += 1

        # 2) Texto completo: es red, así que va antes de abrir la transacción.
        if to_insert:
            await self._add_full_text(feed, to_insert, result)

        # 3) Duplicados entre feeds distintos: se anotan, no se descartan.
        for entry in to_insert:
            twin = repo.content_hash_exists(self.conn, entry.content_hash)
            if twin:
                result.duplicate_of[entry.id] = twin

        # 4) Escritura: un único lote, sin `await` dentro.
        with _write_tx(self.conn):
            for entry in to_insert:
                repo.insert_entry(self.conn, entry)
                result.new_entries.append(entry.id)
                self._notify(entry, feed)
            for entry_id, entry in to_update:
                repo.update_entry_body(
                    self.conn, entry_id, html=entry.body_html, text=entry.body_text
                )
                result.updated += 1
            self._write_meta(
                feed, parsed, fetched, changed=result.changed, at=started,
                watch_hash=watch_hash,
            )

    def _content_changed(self, entry_id: str, entry: Entry) -> bool:
        row = self.conn.execute(
            "SELECT content_hash FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return bool(row) and row["content_hash"] != entry.content_hash

    async def _add_full_text(self, feed: Feed, entries: list[Entry], result: IngestResult) -> None:
        """Baja el artículo completo de las entradas que lo necesiten, en paralelo."""
        pending = [e for e in entries if should_extract(e, feed, self.cfg)]
        if not pending:
            return
        client = self.fetcher._ensure_client()
        limit = anyio.Semaphore(max(1, self.cfg.concurrency))

        async def one(entry: Entry) -> None:
            async with limit:
                html, text = await extract_full_text(client, entry.url or "", self.cfg)
            if text and len(text) > len(entry.body_text or ""):
                entry.body_html = html or entry.body_html
                entry.body_text = text
                entry.full_text_at = now_ms()
                result.full_text += 1

        async with anyio.create_task_group() as tg:
            for entry in pending:
                tg.start_soon(one, entry)

    def _notify(self, entry: Entry, feed: Feed) -> None:
        """Punto de enganche del motor de reglas. Un fallo suyo no aborta la ingesta."""
        if self.on_new_entry is None:
            return
        try:
            self.on_new_entry(self.conn, entry, feed)
        except Exception:
            log.exception("el gancho on_new_entry falló con la entrada %s", entry.id)

    # -- contabilidad del feed ---------------------------------------------
    def _write_meta(
        self,
        feed: Feed,
        parsed: ParsedFeed | None,
        fetched: FetchResult,
        *,
        changed: bool,
        at: int,
        watch_hash: str | None = None,
    ) -> None:
        feed.error_count = 0
        interval = next_interval(feed, changed, self.cfg)
        fields: dict[str, object] = {
            "etag": fetched.etag or feed.etag,
            "last_modified": fetched.last_modified or feed.last_modified,
            "last_fetch_at": at,
            "last_success_at": at,
            "next_fetch_at": now_ms() + interval * 1000,
            "interval_seconds": interval,
            "error_count": 0,
            "last_error": None,
        }
        if watch_hash is not None:
            fields["watch_hash"] = watch_hash
        if parsed is not None:
            # El canal solo rellena huecos: un título puesto por el usuario manda.
            if parsed.title and not feed.title:
                fields["title"] = parsed.title
            if parsed.site_url and not feed.site_url:
                fields["site_url"] = parsed.site_url
            if parsed.description and not feed.description:
                fields["description"] = parsed.description
            if parsed.icon_url and not feed.icon_url:
                fields["icon_url"] = parsed.icon_url
        repo.update_feed_meta(self.conn, feed.id, **fields)
        _apply(feed, fields)
        feed.interval_seconds = interval

    def _record_success(self, feed: Feed, fetched: FetchResult, *, changed: bool, at: int) -> None:
        self._write_meta(feed, None, fetched, changed=changed, at=at)

    def _record_error(self, feed: Feed, message: str, *, at: int) -> None:
        feed.error_count += 1
        interval = next_interval(feed, False, self.cfg)
        fields: dict[str, object] = {
            "last_fetch_at": at,
            "next_fetch_at": now_ms() + interval * 1000,
            "interval_seconds": interval,
            "error_count": feed.error_count,
            "last_error": message[:500],
        }
        repo.update_feed_meta(self.conn, feed.id, **fields)
        _apply(feed, fields)

    # -- lotes ---------------------------------------------------------------
    async def refresh_due(self, limit: int = 1000, *, now: int | None = None) -> list[IngestResult]:
        """Refresca todo lo que toca ahora. Un feed roto no tumba el lote."""
        feeds = repo.due_feeds(self.conn, now, limit)
        if not feeds:
            return []
        results: list[IngestResult] = []
        gate = anyio.Semaphore(max(1, self.cfg.concurrency))

        async def one(feed: Feed) -> None:
            async with gate:
                try:
                    results.append(await self.refresh_feed(feed))
                except Exception as exc:           # red de seguridad final
                    log.exception("refresco abortado en %s", feed.url)
                    results.append(
                        IngestResult(
                            feed_id=feed.id, status="error", error=f"{type(exc).__name__}: {exc}"
                        )
                    )

        async with anyio.create_task_group() as tg:
            for feed in feeds:
                tg.start_soon(one, feed)
        return results

    async def refresh_all(self) -> list[IngestResult]:
        """Fuerza el refresco de todos los feeds activos, ignorando `next_fetch_at`."""
        feeds = [f for f in repo.list_feeds(self.conn) if not f.disabled]
        results: list[IngestResult] = []
        gate = anyio.Semaphore(max(1, self.cfg.concurrency))

        async def one(feed: Feed) -> None:
            async with gate:
                results.append(await self.refresh_feed(feed))

        async with anyio.create_task_group() as tg:
            for feed in feeds:
                tg.start_soon(one, feed)
        return results

    # -- alta de feeds -------------------------------------------------------
    async def add_by_url(
        self,
        url: str,
        folder_id: str | None = None,
        *,
        refresh: bool = True,
        fetch_full_text: bool | None = None,
    ) -> Feed:
        """Da de alta un feed a partir de una URL, que puede ser la del sitio web.

        Si la URL apunta a una página HTML se busca su `<link rel="alternate">`
        de RSS/Atom: el usuario pega `elpais.com`, no la ruta del XML.
        """
        url = url.strip()
        if existing := repo.feed_by_url(self.conn, url):
            return existing

        feed_url, parsed = await self._resolve_feed_url(url)
        if existing := repo.feed_by_url(self.conn, feed_url):
            return existing

        feed = Feed(
            url=feed_url,
            folder_id=folder_id,
            title=(parsed.title if parsed else "") or "",
            site_url=(parsed.site_url if parsed else None),
            description=(parsed.description if parsed else None),
            icon_url=(parsed.icon_url if parsed else None),
            interval_seconds=self.cfg.default_interval_seconds,
            next_fetch_at=0,
            fetch_full_text=(
                self.cfg.full_text_default if fetch_full_text is None else fetch_full_text
            ),
        )
        feed = repo.add_feed(self.conn, feed)
        if refresh:
            await self.refresh_feed(feed)
            feed = repo.get_feed(self.conn, feed.id) or feed
        return feed

    async def add_source(
        self,
        url: str,
        kind: str,
        config: dict,
        *,
        folder_id: str | None = None,
        title: str = "",
        refresh: bool = True,
    ) -> Feed:
        """Da de alta una web raspada o vigilada.

        Se fuerza un intervalo mínimo: raspar es más caro para el servidor ajeno
        que servir un XML, y la web no ha pedido que la visitemos.
        """
        import json

        if existing := repo.feed_by_url(self.conn, url):
            return existing
        feed = Feed(
            url=url,
            folder_id=folder_id,
            title=title,
            site_url=url,
            source_kind=kind,
            source_config_json=json.dumps(config, ensure_ascii=False),
            interval_seconds=max(self.cfg.default_interval_seconds, MIN_SCRAPE_INTERVAL),
            next_fetch_at=0,
        )
        feed = repo.add_feed(self.conn, feed)
        if refresh:
            await self.refresh_feed(feed)
            feed = repo.get_feed(self.conn, feed.id) or feed
        return feed

    async def _probe_common_paths(self, base: str) -> tuple[str, ParsedFeed] | None:
        """Muchas webs publican feed pero no lo enlazan en el HTML.

        Merece la pena agotar esta vía antes de proponer raspar: un feed no se
        rompe cuando rediseñan la web, y el raspado sí.
        """
        from urllib.parse import urljoin, urlparse

        partes = urlparse(base)
        if not partes.scheme or not partes.netloc:
            return None
        raiz = f"{partes.scheme}://{partes.netloc}"
        for ruta in COMMON_FEED_PATHS:
            candidato = urljoin(raiz + "/", ruta.lstrip("/"))
            probe = await self.fetcher.get(candidato)
            if not probe.ok or not probe.content:
                continue
            parsed = parse_feed(
                probe.content, "descubrimiento", base_url=probe.final_url or candidato
            )
            if parsed.is_feed and parsed.entries:
                log.info("feed no enlazado encontrado en %s", candidato)
                return probe.final_url or candidato, parsed
        return None

    async def _resolve_feed_url(self, url: str) -> tuple[str, ParsedFeed | None]:
        """Devuelve la URL real del feed y, si ya la tenemos parseada, su canal."""
        fetched = await self.fetcher.get(url)
        if not fetched.ok or not fetched.content:
            # Sin poder mirar, damos por buena la URL: el primer refresco dirá.
            log.info("no se pudo inspeccionar %s: %s", url, fetched.error)
            return url, None

        final_url = fetched.final_url or url
        parsed = parse_feed(fetched.content, "descubrimiento", base_url=final_url)
        if parsed.is_feed:
            return final_url, parsed

        for candidate in discover_feed_links(fetched.text(), final_url):
            probe = await self.fetcher.get(candidate)
            if not probe.ok or not probe.content:
                continue
            sub = parse_feed(probe.content, "descubrimiento", base_url=probe.final_url or candidate)
            if sub.is_feed:
                return probe.final_url or candidate, sub

        if encontrado := await self._probe_common_paths(final_url):
            return encontrado

        # No hay feed por ninguna vía: se ofrece raspar, con propuestas de
        # selectores para que quien llama pueda enseñar una previsualización.
        raise NoFeedFound(final_url, guess_selectors(fetched.text(), final_url))


# ==================================================================== utilidades
@contextmanager
def _write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Agrupa un lote de escrituras en una transacción explícita.

    La conexión está en autocommit (`isolation_level=None`), así que sin esto
    cada `INSERT` sería su propia transacción y su propio fsync.
    """
    nested = conn.in_transaction
    if not nested:
        conn.execute("BEGIN")
    try:
        yield
    except Exception:
        if not nested and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if not nested and conn.in_transaction:
            conn.execute("COMMIT")


def _apply(feed: Feed, fields: dict[str, object]) -> None:
    """Refleja en el objeto en memoria lo que se acaba de escribir en la base."""
    for key, value in fields.items():
        if hasattr(feed, key):
            setattr(feed, key, value)


def _fetch_cfg(cfg: Config | FetchConfig | None) -> FetchConfig:
    if cfg is None:
        return FetchConfig()
    return cfg.fetch if isinstance(cfg, Config) else cfg
