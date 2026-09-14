"""Interfaz de línea de órdenes: `rss`.

Sirve para administrar el lector sin abrir ninguna UI y es la herramienta con la
que se prueban las fases del proyecto de principio a fin.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import repo
from .config import Config
from .db import open_db
from .models import EntrySelection


def _fecha(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def _conn(args):
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "db", None):
        cfg.db_path = Path(args.db).expanduser()
    conn = open_db(cfg.db_path, device_name=cfg.device_name)
    if hasattr(args, "_connections"):
        args._connections.append(conn)
    return conn, cfg


# ------------------------------------------------------------------ órdenes
def cmd_add(args) -> int:
    from .reader import subscribe

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
        title = asyncio.run(subscribe(conn, cfg, args.url, folder_id))
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
    print(f"Añadido: {title}")
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
    if args.json:
        print(json.dumps([
            {**f.model_dump(mode="json"), "unread": counts.get(f.id, 0)} for f in feeds
        ], ensure_ascii=False))
        return 0
    if not feeds:
        print("No hay ningún feed. Añade uno con:  rss add <url>")
        return 0
    for f in feeds:
        n = counts.get(f.id, 0)
        marca = f"({n})" if n else "   "
        carpeta = f"[{folders.get(f.folder_id, '')}] " if f.folder_id else ""
        error = "  ⚠ " + f.last_error[:40] if f.last_error else ""
        print(f"{marca:>6} {carpeta}{f.display_title}{error}  [{f.id}]")
    total = sum(counts.values())
    print(f"\n{len(feeds)} feeds, {total} sin leer")
    return 0


def cmd_refresh(args) -> int:
    from .reader import refresh

    conn, cfg = _conn(args)
    feed = None
    if args.feed:
        feed = repo.get_feed(conn, args.feed) or repo.feed_by_url(conn, args.feed)
        if not feed:
            print(f"No encuentro el feed: {args.feed}", file=sys.stderr)
            return 1
    print(asyncio.run(refresh(conn, cfg, feed=feed, force=args.all)))
    return 0


def cmd_unread(args) -> int:
    conn, _ = _conn(args)
    sel = EntrySelection(unread_only=True, limit=args.limit)
    if args.feed:
        sel.feed_ids = [args.feed]
    _print_entries(conn, repo.select_entries(conn, sel), args.json)
    return 0


def _print_entries(conn, entries, as_json: bool) -> None:
    from .reader import terminal_text

    if as_json:
        rows = []
        for entry in entries:
            state = repo.get_state(conn, entry.id)
            rows.append({**entry.model_dump(mode="json"),
                         "state": state.model_dump(mode="json") if state else None})
        print(json.dumps(rows, ensure_ascii=False))
        return
    feeds = {f.id: f.display_title for f in repo.list_feeds(conn)}
    for e in entries:
        print(terminal_text(
            f"{_fecha(e.published_at)}  {feeds.get(e.feed_id, '?')[:22]:22}  {e.title}"
        ))
        print(f"{'':24}{e.id}")


def cmd_entries(args) -> int:
    conn, _ = _conn(args)
    selection = EntrySelection(
        feed_ids=[args.feed] if args.feed else [], query=args.query,
        unread_only=args.unread, starred_only=args.starred,
        limit=args.limit, offset=args.offset,
    )
    _print_entries(conn, repo.select_entries(conn, selection), args.json)
    return 0


def cmd_show(args) -> int:
    from .reader import article_text, load_article, terminal_text

    conn, cfg = _conn(args)
    entry = asyncio.run(load_article(conn, cfg, args.id, offline=args.offline))
    if args.mark_read:
        repo.set_read(conn, [entry.id], True)
    if args.json:
        print(json.dumps({**entry.model_dump(mode="json"), "text": article_text(entry)},
                         ensure_ascii=False))
    else:
        print(terminal_text(f"{entry.title}\n{entry.url or ''}\n"))
        print(article_text(entry))
        if not (entry.body_html or entry.body_text):
            print("Solo hay un resumen disponible.", file=sys.stderr)
    return 0


def cmd_tui(args) -> int:
    from .tui import run

    # Rechaza pipes antes de abrir o crear la base.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("rss tui necesita un terminal interactivo; usa rss entries o rss show")
    conn, cfg = _conn(args)
    return run(conn, cfg)


def cmd_read(args) -> int:
    if args.feed and (args.ids or args.unread):
        raise ValueError("--feed no se puede combinar con IDs ni con --unread")
    if not args.feed and not args.ids:
        raise ValueError("Indica los IDs o --feed")
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
    results = repo.search(conn, args.query, args.limit)
    _print_entries(conn, results, args.json)
    if not args.json:
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


def cmd_import_inoreader(args) -> int:
    from .db import ensure_node, migrate
    from .ids import new_id
    from .inoreader import import_archive, read_archive

    archive = read_archive(args.file)
    cfg = Config.load(args.config)
    db_path = Path(args.db).expanduser() if args.db else cfg.db_path
    if args.apply and cfg.hub_url:
        raise ValueError(
            "Importa en la base del hub usando su configuración; "
            "el cliente no sube cuerpos de artículos al servidor"
        )
    # Previsualización sobre una copia coherente en memoria, también con WAL.
    with closing(open_db(":memory:")) as preview:
        if db_path.exists():
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                src.backup(preview)
            migrate(preview)
            ensure_node(preview, cfg.device_name)
        report = import_archive(preview, archive)
    backup = None
    if args.apply:
        if db_path.exists():
            backup = db_path.parent / "backups" / f"before-inoreader-{new_id()}.db"
            backup.parent.mkdir(parents=True, exist_ok=True)
            with (
                closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(backup)) as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("La copia de seguridad no supera la comprobación")
        with closing(open_db(db_path, device_name=cfg.device_name)) as conn:
            report = import_archive(conn, archive)
    result = {"applied": args.apply, "database": str(db_path),
              "archive_sha256": archive.sha256, "backup": str(backup) if backup else None,
              **report.as_dict()}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("Importación completada" if args.apply else "Previsualización: base sin modificar")
        print(f"{report.feeds_new} fuentes nuevas, {report.folders_new} carpetas nuevas")
        print(f"{report.entries_new} artículos nuevos, {report.entries_linked} vinculados, "
              f"{report.entries_skipped} omitidos o ya importados")
        print(f"{report.views_created} vistas creadas para carpetas compartidas")
        if backup:
            print(f"Copia de seguridad: {backup}")
        for warning in report.warnings:
            print(f"Aviso: {warning}")
        if not args.apply:
            print("Añade --apply para importar con copia de seguridad automática.")
    return 0


def cmd_sync(args) -> int:
    from .reader import sync

    conn, cfg = _conn(args)
    hub = args.hub or cfg.hub_url
    if not hub:
        print("Falta la URL del hub (--hub o hub_url en la configuración)", file=sys.stderr)
        return 1
    cfg.hub_url = hub
    stats = asyncio.run(sync(conn, cfg))
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
def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("debe ser mayor que cero")
    return number


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("no puede ser negativo")
    return number


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
    s.add_argument("--json", action="store_true", help="salida JSON para scripts")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("refresh", help="descargar novedades")
    s.add_argument("--feed", help="solo este feed (id o url)")
    s.add_argument("--all", action="store_true", help="todos, ignorando el intervalo")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("unread", help="listar artículos sin leer")
    s.add_argument("--feed")
    s.add_argument("-n", "--limit", type=_positive, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_unread)

    s = sub.add_parser("entries", help="listar el archivo con filtros y paginación")
    s.add_argument("--feed", help="ID del feed")
    s.add_argument("--query", help="consulta FTS5")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--starred", action="store_true")
    s.add_argument("-n", "--limit", type=_positive, default=30)
    s.add_argument("--offset", type=_nonnegative, default=0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_entries)

    s = sub.add_parser("show", help="leer un artículo como texto, sin abrir navegador")
    s.add_argument("id", help="ID obtenido con entries, unread o search")
    s.add_argument("--offline", action="store_true", help="usar únicamente el contenido local")
    s.add_argument("--mark-read", action="store_true", help="marcar leído al mostrarlo")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("tui", help="abrir la interfaz textual interactiva (curses)")
    s.set_defaults(func=cmd_tui)

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
    s.add_argument("-n", "--limit", type=_positive, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("opml", help="importar/exportar suscripciones")
    s.add_argument("action", choices=["import", "export"])
    s.add_argument("file", nargs="?")
    s.set_defaults(func=cmd_opml)

    s = sub.add_parser("import-inoreader", help="importar ZIP de Inoreader (previsualiza primero)")
    s.add_argument("file", help="exportación ZIP")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="importar con copia de seguridad previa")
    mode.add_argument("--dry-run", action="store_true", help="solo previsualizar (predeterminado)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_import_inoreader)

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
    args._connections = []
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except ImportError as exc:
        print(f"Módulo aún no disponible: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, sqlite3.Error, httpx.HTTPError) as exc:
        from .reader import terminal_text

        print(terminal_text(f"Error: {exc}"), file=sys.stderr)
        return 1
    finally:
        for conn in args._connections:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
