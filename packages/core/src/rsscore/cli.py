"""Interfaz de línea de órdenes: `rss`.

Sirve para administrar el lector sin abrir ninguna UI y es la herramienta con la
que se prueban las fases del proyecto de principio a fin.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import repo
from .config import Config
from .db import open_db
from .models import EntrySelection


def _fecha(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def _conn(args):
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "db", None):
        cfg.db_path = Path(args.db)
    return open_db(cfg.db_path, device_name=cfg.device_name), cfg


# ------------------------------------------------------------------ órdenes
def cmd_add(args) -> int:
    from .ingest import Ingestor

    conn, cfg = _conn(args)
    folder_id = None
    if args.folder:
        folder = repo.folder_by_name(conn, args.folder)
        if not folder:
            from .models import Folder

            folder = repo.upsert_folder(conn, Folder(name=args.folder))
        folder_id = folder.id
    from .ingest import NoFeedFound

    try:
        feed = asyncio.run(Ingestor(conn, cfg).add_by_url(args.url, folder_id=folder_id))
    except NoFeedFound as exc:
        print(f"{args.url} no publica ningún feed.", file=sys.stderr)
        if exc.candidates:
            mejor = exc.candidates[0]
            print(
                f"Pero parece que se puede raspar: «{mejor.config.item_selector}» "
                f"encuentra {mejor.count} artículos.",
                file=sys.stderr,
            )
            for m in mejor.sample[:3]:
                print(f"    · {m[:66]}", file=sys.stderr)
            print(f"\n  Pruébalo con:  rss scrape {args.url} --preview", file=sys.stderr)
        else:
            print(f"  Para vigilar los cambios:  rss watch {args.url}", file=sys.stderr)
        return 1
    print(f"Añadido: {feed.display_title}  [{feed.id}]")
    print(f"  {feed.url}")
    return 0


def cmd_scrape(args) -> int:
    """Da de alta una web sin feed, raspando su listado de artículos."""

    from .ingest import Ingestor
    from .scrape import ScrapeConfig, guess_selectors, looks_javascript_rendered, scrape_page

    conn, cfg = _conn(args)

    async def trabajo():
        async with Ingestor(conn, cfg) as ing:
            respuesta = await ing.fetcher.get(args.url)
            if not respuesta.ok or not respuesta.content:
                print(f"No se pudo descargar {args.url}: {respuesta.error}", file=sys.stderr)
                return 1
            html = respuesta.text()
            base = respuesta.final_url or args.url

            if args.selector:
                config = ScrapeConfig(
                    item_selector=args.selector,
                    title_selector=args.title_selector or "",
                    date_selector=args.date_selector or "",
                )
            else:
                candidatos = guess_selectors(html, base)
                if not candidatos:
                    if looks_javascript_rendered(html):
                        print(
                            "Esta página construye su contenido con JavaScript, así que "
                            "descargar el HTML no basta.\nPrueba con un puente tipo "
                            "RSS-Bridge, o pasa --selector si sabes dónde mirar.",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            "No he sabido reconocer un listado de artículos.\n"
                            "Indica el selector CSS a mano con --selector.",
                            file=sys.stderr,
                        )
                    return 1
                if args.preview:
                    print(f"Propuestas para {base}:\n")
                    for i, c in enumerate(candidatos, 1):
                        print(f"  [{i}] {c.config.item_selector}   ({c.count} elementos, "
                              f"confianza {c.score:.1f})")
                        for m in c.sample:
                            print(f"        · {m[:70]}")
                        print()
                config = candidatos[0].config

            try:
                parsed = scrape_page(html, "previsualizacion", config, base_url=base)
            except Exception as exc:
                print(f"El raspado no funcionó: {exc}", file=sys.stderr)
                return 1

            if args.preview:
                print(f"Con «{config.item_selector}» se extraerían {len(parsed.entries)} "
                      "artículos:\n")
                for e in parsed.entries[:10]:
                    print(f"  {_fecha(e.published_at)}  {e.title[:56]}")
                    print(f"{'':20}{e.url or '(sin enlace)'}")
                print("\nSi te convence, repite sin --preview para darlo de alta.")
                return 0

            folder_id = _carpeta(conn, args.folder)
            feed = await ing.add_source(
                base, "scrape", config.model_dump(), folder_id=folder_id,
                title=parsed.title or base,
            )
            entradas = conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE feed_id = ?", (feed.id,)
            ).fetchone()["n"]
            print(f"Añadido (raspado): {feed.display_title}  [{feed.id}]")
            print(f"  selector: {config.item_selector}")
            print(f"  {entradas} artículos en el primer refresco")
            return 0

    return asyncio.run(trabajo())


def cmd_watch(args) -> int:
    """Vigila una página y crea una entrada cada vez que cambie."""
    from .ingest import Ingestor
    from .scrape import WatchConfig

    conn, cfg = _conn(args)
    config = WatchConfig(
        selector=args.selector or "",
        ignore_selectors=args.ignore or [],
        mode="html" if args.html else "text",
    )

    async def trabajo():
        async with Ingestor(conn, cfg) as ing:
            folder_id = _carpeta(conn, args.folder)
            feed = await ing.add_source(
                args.url, "watch", config.model_dump(), folder_id=folder_id,
                title=args.title or args.url,
            )
            actualizado = repo.get_feed(conn, feed.id)
            if actualizado and actualizado.last_error:
                print(f"Aviso: {actualizado.last_error}", file=sys.stderr)
                return 1
            print(f"Vigilando: {feed.display_title}  [{feed.id}]")
            print(f"  zona: {config.selector or 'la página entera'}")
            print(f"  se revisará cada {feed.interval_seconds // 60} minutos")
            return 0

    return asyncio.run(trabajo())


def _carpeta(conn, nombre: str | None) -> str | None:
    if not nombre:
        return None
    from .models import Folder

    carpeta = repo.folder_by_name(conn, nombre)
    if carpeta is None:
        carpeta = repo.upsert_folder(conn, Folder(name=nombre))
    return carpeta.id


def cmd_list(args) -> int:
    conn, _ = _conn(args)
    counts = repo.unread_counts(conn)
    folders = {f.id: f.name for f in repo.list_folders(conn)}
    feeds = repo.list_feeds(conn)
    if not feeds:
        print("No hay ningún feed. Añade uno con:  rss add <url>")
        return 0
    for f in feeds:
        n = counts.get(f.id, 0)
        marca = f"({n})" if n else "   "
        carpeta = f"[{folders.get(f.folder_id, '')}] " if f.folder_id else ""
        error = "  ⚠ " + f.last_error[:40] if f.last_error else ""
        print(f"{marca:>6} {carpeta}{f.display_title}{error}")
    total = sum(counts.values())
    print(f"\n{len(feeds)} feeds, {total} sin leer")
    return 0


def cmd_refresh(args) -> int:
    from .ingest import Ingestor

    conn, cfg = _conn(args)
    ingestor = Ingestor(conn, cfg, on_new_entry=_rules_hook(conn, cfg))
    if args.feed:
        feed = repo.get_feed(conn, args.feed) or repo.feed_by_url(conn, args.feed)
        if not feed:
            print(f"No encuentro el feed: {args.feed}", file=sys.stderr)
            return 1
        results = [asyncio.run(ingestor.refresh_feed(feed))]
    elif args.all:
        feeds = repo.list_feeds(conn)
        results = asyncio.run(_refresh_many(ingestor, feeds))
    else:
        results = asyncio.run(ingestor.refresh_due())
    nuevas = sum(len(r.new_entries) for r in results)
    errores = [r for r in results if r.status == "error"]
    print(f"{len(results)} feeds refrescados, {nuevas} entradas nuevas, {len(errores)} con error")
    for r in errores[:10]:
        print(f"  ⚠ {r.feed_id}: {r.error}")
    return 0


async def _refresh_many(ingestor, feeds):
    out = []
    for feed in feeds:
        out.append(await ingestor.refresh_feed(feed))
    return out


def _rules_hook(conn, cfg):
    try:
        from .rules.apply import apply_rules
        from .rules.engine import RuleEngine
        from .rules.store import load_rules
    except ImportError:
        return None
    rules = load_rules(conn)
    if not rules:
        return None
    engine = RuleEngine(rules)

    def hook(c, entry, feed):
        try:
            apply_rules(c, entry, feed, engine)
        except Exception as exc:  # una regla rota no puede parar la ingesta
            print(f"  ⚠ regla falló en «{entry.title[:40]}»: {exc}", file=sys.stderr)

    return hook


def cmd_unread(args) -> int:
    conn, _ = _conn(args)
    sel = EntrySelection(unread_only=True, limit=args.limit)
    if args.feed:
        sel.feed_ids = [args.feed]
    feeds = {f.id: f.display_title for f in repo.list_feeds(conn)}
    for e in repo.select_entries(conn, sel):
        print(f"{_fecha(e.published_at)}  {feeds.get(e.feed_id, '?')[:22]:22}  {e.title}")
        print(f"{'':24}{e.id}")
    return 0


def cmd_read(args) -> int:
    conn, _ = _conn(args)
    if args.feed:
        n = repo.mark_feed_read(conn, args.feed)
    else:
        n = repo.set_read(conn, args.ids, not args.unread)
    print(f"{n} artículos actualizados")
    return 0


def cmd_star(args) -> int:
    conn, _ = _conn(args)
    n = repo.set_starred(conn, args.ids, not args.remove)
    print(f"{n} artículos actualizados")
    return 0


def cmd_search(args) -> int:
    conn, _ = _conn(args)
    feeds = {f.id: f.display_title for f in repo.list_feeds(conn)}
    results = repo.search(conn, args.query, args.limit)
    for e in results:
        print(f"{_fecha(e.published_at)}  {feeds.get(e.feed_id, '?')[:22]:22}  {e.title}")
    print(f"\n{len(results)} resultados")
    return 0


def cmd_opml(args) -> int:
    from .opml import export_opml, import_opml

    conn, _ = _conn(args)
    if args.action == "import":
        data = Path(args.file).read_bytes()
        result = import_opml(conn, data)
        print(f"Importado: {result}")
    else:
        xml = export_opml(conn)
        if args.file:
            Path(args.file).write_text(xml, encoding="utf-8")
            print(f"Escrito en {args.file}")
        else:
            print(xml)
    return 0


def cmd_sync(args) -> int:
    from .sync import SyncClient

    conn, cfg = _conn(args)
    hub = args.hub or cfg.hub_url
    if not hub:
        print("Falta la URL del hub (--hub o hub_url en la configuración)", file=sys.stderr)
        return 1
    token = cfg.hub_token.get_secret_value()
    stats = asyncio.run(SyncClient(conn, hub, token).sync_once())
    print(f"Sincronizado: {stats}")
    return 0


def cmd_export(args) -> int:
    conn, cfg = _conn(args)
    if args.kind == "obsidian":
        from .export.obsidian import export_to_obsidian

        if not cfg.obsidian.vault_path:
            print("Configura obsidian.vault_path en el config.yaml", file=sys.stderr)
            return 1
        paths = export_to_obsidian(conn, args.ids, cfg.obsidian)
        for p in paths:
            print(f"Escrito: {p}")
    elif args.kind == "kindle":
        from .export.kindle import send_to_kindle

        asyncio.run(send_to_kindle(conn, args.ids, cfg.smtp))
        print(f"Enviados {len(args.ids)} artículos al Kindle (EPUB)")
    elif args.kind == "magazine":
        from .export.magazine import build_magazine

        sel = EntrySelection(
            entry_ids=args.ids, unread_only=args.unread, limit=cfg.magazine.max_articles
        )
        result = build_magazine(conn, sel, cfg.magazine)
        print(f"Revista generada: {getattr(result, 'path', result)}")
    return 0


def cmd_rules(args) -> int:
    from .rules.store import export_rules_yaml, import_rules_yaml, load_rules

    conn, _ = _conn(args)
    if args.action == "list":
        for r in load_rules(conn):
            estado = "on " if r.enabled else "off"
            print(f"[{estado}] {r.name}  ({len(r.then)} acciones)")
    elif args.action == "import":
        n = import_rules_yaml(conn, Path(args.file).read_text(encoding="utf-8"))
        print(f"{n} reglas importadas")
    elif args.action == "export":
        print(export_rules_yaml(conn))
    return 0


def cmd_backfill(args) -> int:
    """Aplica las reglas a lo ya descargado.

    Al escribir una regla nueva uno espera que actúe también sobre el archivo,
    no solo sobre lo que llegue a partir de ahora.
    """
    from .rules import RuleEngine, apply_rules, load_rules

    conn, _ = _conn(args)
    reglas = load_rules(conn)
    if args.rule:
        reglas = [r for r in reglas if r.name == args.rule or r.id == args.rule]
    if not reglas:
        print("No hay reglas que aplicar", file=sys.stderr)
        return 1

    engine = RuleEngine(reglas)
    entradas = repo.select_entries(conn, EntrySelection(limit=args.limit))
    afectadas = 0
    for entrada in entradas:
        feed = repo.get_feed(conn, entrada.feed_id)
        if feed is None:
            continue
        entrada.body_html, entrada.body_text = repo.get_body(conn, entrada.id)
        outcome = apply_rules(conn, entrada, feed, engine)
        if outcome.applied_rules:
            afectadas += 1
            marcas = []
            if outcome.tags_added:
                marcas.append("+" + ",".join(outcome.tags_added))
            if outcome.starred:
                marcas.append("★")
            if outcome.marked_read:
                marcas.append("leído")
            print(f"  {entrada.title[:58]:60} {' '.join(marcas)}  ← {outcome.applied_rules[0]}")
    print(f"\n{len(entradas)} artículos revisados, {afectadas} afectados")
    return 0


def cmd_stats(args) -> int:
    conn, cfg = _conn(args)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    tam = Path(cfg.db_path).stat().st_size / 1e6 if Path(cfg.db_path).exists() else 0
    print(f"Base de datos : {cfg.db_path}  ({tam:.1f} MB)")
    print(f"Feeds         : {q('SELECT COUNT(*) FROM feeds WHERE deleted = 0')}")
    print(f"Entradas      : {q('SELECT COUNT(*) FROM entries')}")
    print(f"Sin leer      : {q('SELECT COUNT(*) FROM entry_state WHERE read = 0')}")
    print(f"Guardadas     : {q('SELECT COUNT(*) FROM entry_state WHERE starred = 1')}")
    print(f"Etiquetas     : {q('SELECT COUNT(*) FROM tags WHERE deleted = 0')}")
    print(f"Diario cambios: {q('SELECT COUNT(*) FROM change_log')} ops")
    cuerpos = q("SELECT COALESCE(SUM(LENGTH(html_zstd) + LENGTH(text_zstd)), 0) FROM entry_bodies")
    crudo = q("SELECT COALESCE(SUM(bytes_raw), 0) FROM entry_bodies")
    if crudo:
        print(
            f"Cuerpos       : {cuerpos / 1e6:.1f} MB comprimidos de {crudo / 1e6:.1f} MB "
            f"({crudo / max(cuerpos, 1):.1f}x)"
        )
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rss", description="Lector RSS: administración por consola")
    p.add_argument("--config", help="ruta al config.yaml")
    p.add_argument("--db", help="ruta a la base de datos (tiene prioridad)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="suscribirse a un feed (acepta la URL de la web)")
    s.add_argument("url")
    s.add_argument("-f", "--folder", help="carpeta destino")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("scrape", help="suscribirse a una web SIN feed, raspando su listado")
    s.add_argument("url")
    s.add_argument("--preview", action="store_true",
                   help="solo enseñar qué se extraería, sin dar de alta")
    s.add_argument("--selector", help="selector CSS de cada artículo (si no, se deduce)")
    s.add_argument("--title-selector", help="selector del título dentro de cada artículo")
    s.add_argument("--date-selector", help="selector de la fecha")
    s.add_argument("-f", "--folder", help="carpeta destino")
    s.set_defaults(func=cmd_scrape)

    s = sub.add_parser("watch", help="vigilar una página y avisar cuando cambie")
    s.add_argument("url")
    s.add_argument("--selector", help="zona a vigilar (si no, la página entera)")
    s.add_argument("--ignore", action="append",
                   help="selector a ignorar; repetible (anuncios, «actualizado el…»)")
    s.add_argument("--html", action="store_true", help="vigilar el HTML, no solo el texto")
    s.add_argument("--title", help="nombre para la suscripción")
    s.add_argument("-f", "--folder", help="carpeta destino")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("list", help="listar suscripciones")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("refresh", help="descargar novedades")
    s.add_argument("--feed", help="solo este feed (id o url)")
    s.add_argument("--all", action="store_true", help="todos, ignorando el intervalo")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("unread", help="listar artículos sin leer")
    s.add_argument("--feed")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(func=cmd_unread)

    s = sub.add_parser("read", help="marcar como leído")
    s.add_argument("ids", nargs="*")
    s.add_argument("--feed", help="marcar todo un feed")
    s.add_argument("--unread", action="store_true", help="marcar como NO leído")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("star", help="guardar artículos")
    s.add_argument("ids", nargs="+")
    s.add_argument("--remove", action="store_true")
    s.set_defaults(func=cmd_star)

    s = sub.add_parser("search", help="búsqueda full-text")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("opml", help="importar/exportar suscripciones")
    s.add_argument("action", choices=["import", "export"])
    s.add_argument("file", nargs="?")
    s.set_defaults(func=cmd_opml)

    s = sub.add_parser("sync", help="sincronizar con el hub")
    s.add_argument("--hub", help="URL del hub")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("export", help="exportar artículos")
    s.add_argument("kind", choices=["obsidian", "kindle", "magazine"])
    s.add_argument("ids", nargs="*")
    s.add_argument("--unread", action="store_true", help="revista con lo no leído")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("rules", help="gestionar reglas de filtrado")
    s.add_argument("action", choices=["list", "import", "export"])
    s.add_argument("file", nargs="?")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("backfill", help="aplicar las reglas al archivo ya descargado")
    s.add_argument("--rule", help="solo esta regla (nombre o id)")
    s.add_argument("-n", "--limit", type=int, default=5000)
    s.set_defaults(func=cmd_backfill)

    s = sub.add_parser("stats", help="estado de la base de datos")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except ImportError as exc:
        print(f"Módulo aún no disponible: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
