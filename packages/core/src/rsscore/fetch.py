"""Descarga HTTP de feeds: peticiones condicionales, límites y ritmo adaptativo.

Tres ideas gobiernan este módulo:

* **No volver a bajar lo que no ha cambiado.** Casi todos los servidores admiten
  `ETag`/`If-Modified-Since`; un 304 cuesta unos cientos de bytes frente a los
  cientos de kilobytes de un feed completo. Con 500 feeds cada media hora, la
  diferencia entre usarlo y no usarlo son gigabytes al mes.
* **Nunca confiar en el tamaño de la respuesta.** El cuerpo se lee por trozos y
  se aborta en cuanto supera `max_body_bytes`, para que un servidor hostil (o un
  feed accidentalmente enorme) no se coma la memoria del proceso.
* **Repartir la carga en el tiempo.** `next_interval` sube el intervalo cuando el
  feed no trae novedades y lo baja cuando sí, y añade jitter para que 500 feeds
  dados de alta el mismo día no se sincronicen en el mismo segundo para siempre.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Literal

import anyio
import httpx

from .config import FetchConfig
from .models import Feed

FetchStatus = Literal["ok", "not_modified", "error"]

# Crecimiento/decrecimiento del intervalo entre refrescos.
GROWTH_FACTOR = 1.5
SHRINK_FACTOR = 0.5
JITTER = 0.10          # ±10 %
MAX_ERROR_SHIFT = 6    # el backoff exponencial se satura en 2**6 = 64x

_XML_DECL_ENCODING = re.compile(rb"""<\?xml[^>]*encoding=["']([\w.-]+)["']""", re.IGNORECASE)
_META_CHARSET = re.compile(rb"""<meta[^>]*charset=["']?([\w.-]+)""", re.IGNORECASE)


@dataclass(slots=True)
class FetchResult:
    """Resultado de una descarga. Nunca lleva excepciones: los fallos son datos."""

    status: FetchStatus
    content: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    final_url: str = ""
    error: str | None = None
    http_status: int | None = None
    encoding: str | None = None
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def text(self) -> str:
        """Decodifica el cuerpo con la mejor codificación disponible."""
        if not self.content:
            return ""
        return decode_body(self.content, self.encoding)


def detect_encoding(content: bytes, declared: str | None = None) -> str:
    """Deduce la codificación: cabecera HTTP, declaración XML, `<meta charset>`, utf-8."""
    if declared:
        return declared
    if match := _XML_DECL_ENCODING.search(content[:512]):
        return match.group(1).decode("ascii", "ignore")
    if match := _META_CHARSET.search(content[:2048]):
        return match.group(1).decode("ascii", "ignore")
    return "utf-8"


def decode_body(content: bytes, declared: str | None = None) -> str:
    """Decodifica sin lanzar nunca: latin-1 acepta cualquier byte como último recurso."""
    for enc in (detect_encoding(content, declared), "utf-8", "latin-1"):
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


# ------------------------------------------------------------------- descarga
async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    cfg: FetchConfig,
    *,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """GET con límite de tamaño, timeout y seguimiento de redirecciones."""
    req_headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, "
                  "text/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }
    req_headers.update(headers or {})

    try:
        request = client.build_request(
            "GET", url, headers=req_headers, timeout=httpx.Timeout(cfg.timeout_seconds)
        )
        response = await client.send(request, stream=True, follow_redirects=True)
    except Exception as exc:                       # red caída, DNS, TLS, timeout…
        return FetchResult(status="error", final_url=url, error=_describe(exc))

    try:
        final_url = str(response.url)
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        content_type = response.headers.get("content-type", "")

        if response.status_code == 304:
            return FetchResult(
                status="not_modified", etag=etag, last_modified=last_modified,
                final_url=final_url, http_status=304, content_type=content_type,
            )
        if response.status_code >= 400:
            return FetchResult(
                status="error", final_url=final_url, http_status=response.status_code,
                error=f"HTTP {response.status_code}", content_type=content_type,
            )

        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > cfg.max_body_bytes:
                    return FetchResult(
                        status="error", final_url=final_url, http_status=response.status_code,
                        error=f"cuerpo demasiado grande (> {cfg.max_body_bytes} bytes)",
                        content_type=content_type,
                    )
                chunks.append(chunk)
        except Exception as exc:                   # corte a mitad del cuerpo
            return FetchResult(
                status="error", final_url=final_url, http_status=response.status_code,
                error=_describe(exc), content_type=content_type,
            )

        content = b"".join(chunks)
        return FetchResult(
            status="ok", content=content, etag=etag, last_modified=last_modified,
            final_url=final_url, http_status=response.status_code,
            encoding=detect_encoding(content, response.charset_encoding),
            content_type=content_type,
        )
    finally:
        await response.aclose()


async def fetch_feed(client: httpx.AsyncClient, feed: Feed, cfg: FetchConfig) -> FetchResult:
    """Descarga un feed usando la validación condicional que guarda el propio feed."""
    headers: dict[str, str] = {}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified
    return await fetch_url(client, feed.url, cfg, headers=headers)


def _describe(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# -------------------------------------------------------------------- cliente
@dataclass
class Fetcher:
    """Cliente HTTP reutilizable con concurrencia acotada.

    Un único `AsyncClient` para todo el lote: reaprovecha conexiones TCP/TLS y
    multiplexa por HTTP/2 con los servidores que lo admiten, que es lo que hace
    que refrescar 500 feeds no signifique 500 handshakes.
    """

    cfg: FetchConfig = field(default_factory=FetchConfig)
    client: httpx.AsyncClient | None = None
    _owns_client: bool = field(default=False, init=False, repr=False)
    _sem: anyio.Semaphore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._owns_client = self.client is None

    # -- ciclo de vida ------------------------------------------------------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(self.cfg.timeout_seconds),
                headers={"User-Agent": self.cfg.user_agent},
                limits=httpx.Limits(
                    max_connections=max(1, self.cfg.concurrency),
                    max_keepalive_connections=max(1, self.cfg.concurrency),
                ),
            )
            self._owns_client = True
        return self.client

    def _semaphore(self) -> anyio.Semaphore:
        # Perezoso: el semáforo se ata al bucle de eventos donde se usa.
        if self._sem is None:
            self._sem = anyio.Semaphore(max(1, self.cfg.concurrency))
        return self._sem

    async def __aenter__(self) -> Fetcher:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self.client is not None and self._owns_client:
            await self.client.aclose()
            self.client = None
            self._owns_client = False

    # -- descargas ----------------------------------------------------------
    async def fetch(self, feed: Feed) -> FetchResult:
        client = self._ensure_client()
        async with self._semaphore():
            return await fetch_feed(client, feed, self.cfg)

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
        client = self._ensure_client()
        async with self._semaphore():
            return await fetch_url(client, url, self.cfg, headers=headers)


# Cortesía con servidores ajenos al raspar páginas web.
MIN_SCRAPE_INTERVAL = 1800

# ------------------------------------------------------------ ritmo adaptativo
def next_interval(feed: Feed, changed: bool, cfg: FetchConfig, *, jitter: bool = True) -> int:
    """Calcula los segundos hasta el siguiente refresco.

    * fuentes raspadas → nunca por debajo de `MIN_SCRAPE_INTERVAL`, por cortesía.
    * `feed.error_count > 0` → backoff exponencial sobre el intervalo por defecto.
      Quien llama incrementa el contador antes de invocar esta función.
    * `changed` → el feed publica; nos acercamos al intervalo mínimo.
    * sin novedades → el intervalo crece ×1.5 hasta `max_interval_seconds`.

    El jitter (±10 %) es lo que evita que todos los feeds dados de alta el mismo
    día acaben pidiendo en el mismo instante para siempre.
    """
    minimum = max(60, cfg.default_interval_seconds)
    if feed.source_kind in ("scrape", "watch"):
        # Raspar una web cuesta más al servidor ajeno que servir un XML, y esa
        # web no ha pedido que la visitemos: nunca por debajo de media hora.
        minimum = max(minimum, MIN_SCRAPE_INTERVAL)
    maximum = max(minimum, cfg.max_interval_seconds)
    current = feed.interval_seconds or minimum

    if feed.error_count > 0:
        shift = min(feed.error_count - 1, MAX_ERROR_SHIFT)
        base = minimum * (2**shift)
    elif changed:
        base = max(minimum, current * SHRINK_FACTOR)
    else:
        base = current * GROWTH_FACTOR

    base = min(max(base, minimum), maximum)
    if jitter:
        base *= 1.0 + random.uniform(-JITTER, JITTER)
    # El suelo se aplica DESPUÉS del jitter: en un feed da igual adelantarse un
    # 10 %, pero el mínimo de cortesía al raspar una web ajena es un compromiso
    # y no puede saltárselo un número aleatorio.
    piso = MIN_SCRAPE_INTERVAL if feed.source_kind in ("scrape", "watch") else 60
    return int(max(piso, min(base, maximum * (1 + JITTER))))
