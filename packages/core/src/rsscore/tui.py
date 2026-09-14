"""Lector curses: suscripciones, archivo paginado y lectura a pantalla completa."""

from __future__ import annotations

import asyncio
import curses
import locale
import sqlite3
import textwrap
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, field

import httpx

from . import reader, repo
from .config import Config
from .models import Entry, EntrySelection

PAGE_SIZE = 100


def clip_columns(value: str, width: int) -> str:
    clipped, used = "", 0
    for ch in value:
        size = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        )
        if used + size > width:
            break
        clipped += ch
        used += size
    return clipped


def wrap_columns(value: str, width: int) -> list[str]:
    lines = []
    for paragraph in value.splitlines():
        for line in textwrap.wrap(paragraph, width=max(2, width)) or [""]:
            while line:
                part = clip_columns(line, max(2, width))
                lines.append(part)
                line = line[len(part):]
            if not paragraph.strip():
                lines.append("")
    return lines


@dataclass
class Library:
    """Estado de navegación separado del terminal para comprobarlo sin pantalla."""

    conn: sqlite3.Connection
    feed_id: str | None = None
    query: str = ""
    unread: bool = False
    starred: bool = False
    offset: int = 0
    index: int = 0
    entries: list[Entry] = field(default_factory=list)
    has_more: bool = False

    @property
    def selected(self) -> Entry | None:
        return self.entries[self.index] if self.entries else None

    def reload(self) -> None:
        rows = repo.select_entries(self.conn, EntrySelection(
            feed_ids=[self.feed_id] if self.feed_id else [], query=self.query or None,
            unread_only=self.unread, starred_only=self.starred,
            offset=self.offset, limit=PAGE_SIZE + 1,
        ))
        self.has_more = len(rows) > PAGE_SIZE
        self.entries = rows[:PAGE_SIZE]
        self.index = min(self.index, max(0, len(self.entries) - 1))

    def filter(self, **changes) -> None:
        previous = {key: getattr(self, key) for key in changes}
        previous.update(offset=self.offset, index=self.index)
        for key, value in changes.items():
            setattr(self, key, value)
        self.offset = self.index = 0
        try:
            self.reload()
        except sqlite3.Error:
            for key, value in previous.items():
                setattr(self, key, value)
            raise

    def page(self, direction: int) -> None:
        if direction > 0 and not self.has_more:
            return
        self.offset = max(0, self.offset + direction * PAGE_SIZE)
        self.index = 0
        self.reload()

    def move(self, delta: int) -> None:
        self.index = max(0, min(self.index + delta, len(self.entries) - 1))

    def toggle(self, flag: str, entry_id: str | None = None) -> None:
        target = entry_id or (self.selected.id if self.selected else None)
        state = repo.get_state(self.conn, target) if target else None
        if state:
            if flag == "read":
                repo.set_read(self.conn, [target], not state.read)
            elif flag == "starred":
                repo.set_starred(self.conn, [target], not state.starred)


class TerminalReader:
    def __init__(self, screen, conn, cfg: Config):
        self.screen, self.conn, self.cfg = screen, conn, cfg
        self.library = Library(conn)
        self.feeds = []
        self.feed_index = 0
        self.focus = "entries"
        self.article: Entry | None = None
        self.scroll = 0
        self.help = False
        self.message = "a: añadir fuente | ?: ayuda | q: salir"
        self.reload()

    def reload(self):
        self.feeds = repo.list_feeds(self.conn)
        ids = [None, *(f.id for f in self.feeds)]
        if self.library.feed_id not in ids:
            self.library.filter(feed_id=None)
        self.feed_index = ids.index(self.library.feed_id)
        self.library.reload()

    def put(self, y, x, value, width=None, attr=0):
        height, columns = self.screen.getmaxyx()
        if y < 0 or y >= height or x >= columns:
            return
        width = min(width if width is not None else columns - x - 1, columns - x - 1)
        if width <= 0:
            return
        value = reader.terminal_text(str(value)).replace("\n", " ")
        # ncurses mide columnas; addnstr limita caracteres. El terminal puede
        # cambiar de tamaño entre getmaxyx y la escritura (o contener CJK).
        with suppress(curses.error):
            self.screen.addstr(y, x, clip_columns(value, width), attr)

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 10 or width < 45:
            self.put(0, 0, "Amplía el terminal a 45x10; q para salir")
            self.screen.refresh()
            return
        if self.help:
            lines = [
                "Lector RSS — atajos (cualquier tecla vuelve)",
                "Tab: cambiar panel; j/k o flechas: navegar",
                "Enter: abrir artículo; q/Esc: volver o salir",
                "u: solo sin leer; g: solo guardados",
                "/: buscar con FTS5; búsqueda vacía: limpiar",
                "[ / ] o RePág/AvPág: páginas del archivo",
                "r: alternar leído; s: alternar guardado",
                "R: refrescar fuente/vista; S: sincronizar hub",
                "a: añadir suscripción por URL",
                "Lectura: j/k, Espacio/AvPág, b/RePág, Inicio",
                "Abrir marca leído; S envía cambios al hub",
            ]
            for y, line in enumerate(lines):
                self.put(y, 0, line)
            self.screen.refresh()
            return
        if self.article:
            self.put(0, 0, self.article.title, attr=curses.A_BOLD)
            content = f"{self.article.url or ''}\n\n{reader.article_text(self.article)}"
            lines = wrap_columns(content, width - 2)
            self.scroll = min(self.scroll, max(0, len(lines) - (height - 4)))
            for row, line in enumerate(lines[self.scroll:self.scroll + height - 4], 2):
                self.put(row, 0, line)
            footer = "j/k: desplazar | Espacio: página | r: leído | s: guardar | q: volver"
        else:
            split = min(30, width // 3)
            lib = self.library
            filters = f"{' sin leer' if lib.unread else ''}{' guardados' if lib.starred else ''}"
            self.put(0, 0, "Lector RSS", attr=curses.A_BOLD)
            self.put(0, split + 1, f"Archivo{filters} | {lib.query or 'todos'}")
            counts = repo.unread_counts(self.conn)
            labels = [f"Todas ({sum(counts.values())})", *[
                f"{f.display_title} ({counts.get(f.id, 0)})" for f in self.feeds
            ]]
            visible = height - 4
            start = max(0, self.feed_index - visible + 1)
            for i in range(start, min(len(labels), start + visible)):
                attr = curses.A_REVERSE if self.focus == "feeds" and i == self.feed_index else 0
                self.put(i - start + 2, 0, labels[i], split - 1, attr)
            start = (lib.index // visible) * visible
            for i in range(start, min(len(lib.entries), start + visible)):
                entry = lib.entries[i]
                state = repo.get_state(self.conn, entry.id)
                mark = " " if state and state.read else "•"
                star = "*" if state and state.starred else " "
                attr = curses.A_REVERSE if self.focus == "entries" and i == lib.index else 0
                self.put(i - start + 2, split + 1, f"{mark}{star} {entry.title}", attr=attr)
            if not lib.entries:
                self.put(2, split + 1, "Sin artículos en esta vista")
            footer = (
                f"Pág. {lib.offset // PAGE_SIZE + 1} | Tab: panel | Enter: leer | "
                "/: buscar | ?: ayuda"
            )
        self.put(height - 2, 0, self.message)
        self.put(height - 1, 0, footer, attr=curses.A_BOLD)
        self.screen.refresh()

    def prompt(self, label: str) -> str | None:
        value = ""
        while True:
            self.draw()
            height, _ = self.screen.getmaxyx()
            self.screen.move(height - 2, 0)
            self.screen.clrtoeol()
            self.put(height - 2, 0, f"{label}: {value}")
            self.screen.refresh()
            key = self.screen.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                return value.strip()
            if key == "\x1b":
                return None
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                value = value[:-1]
            elif isinstance(key, str) and key.isprintable():
                value += key

    def busy(self, message, operation):
        self.message = message
        self.draw()
        return asyncio.run(operation)

    def open_selected(self):
        entry = self.library.selected
        if not entry:
            return
        try:
            article = self.busy("Cargando artículo…", reader.load_article(
                self.conn, self.cfg, entry.id,
            ))
            self.message = (
                "" if article.body_text or article.body_html else "Solo hay un resumen disponible"
            )
        except httpx.HTTPError:
            article = repo.get_entry(self.conn, entry.id)
            self.message = "Hub no disponible: mostrando el contenido local"
        self.article, self.scroll = article, 0
        if article:
            repo.set_read(self.conn, [article.id], True)

    def handle(self, key) -> bool:
        if self.help:
            self.help = False
            return True
        lib = self.library
        height, _ = self.screen.getmaxyx()
        if key in ("q", "\x1b"):
            if self.article:
                self.article = None
                self.reload()
                return True
            return False
        if key == "?":
            self.help = True
        elif key in ("r", "s"):
            lib.toggle("read" if key == "r" else "starred",
                       self.article.id if self.article else None)
            if not self.article:
                lib.reload()
            self.message = "Estado actualizado; S sincroniza con el hub"
        elif self.article:
            if key in ("j", curses.KEY_DOWN):
                self.scroll += 1
            elif key in ("k", curses.KEY_UP):
                self.scroll = max(0, self.scroll - 1)
            elif key in (" ", curses.KEY_NPAGE):
                self.scroll += max(1, height - 4)
            elif key in ("b", curses.KEY_PPAGE):
                self.scroll = max(0, self.scroll - max(1, height - 4))
            elif key == curses.KEY_HOME:
                self.scroll = 0
        elif key == "\t":
            self.focus = "feeds" if self.focus == "entries" else "entries"
        elif key in ("j", "k", curses.KEY_DOWN, curses.KEY_UP):
            delta = 1 if key in ("j", curses.KEY_DOWN) else -1
            if self.focus == "feeds":
                self.feed_index = max(0, min(len(self.feeds), self.feed_index + delta))
                feed_id = self.feeds[self.feed_index - 1].id if self.feed_index else None
                lib.filter(feed_id=feed_id)
            else:
                lib.move(delta)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if self.focus == "feeds":
                self.focus = "entries"
            else:
                self.open_selected()
        elif key in ("]", curses.KEY_NPAGE, "[", curses.KEY_PPAGE):
            lib.page(1 if key in ("]", curses.KEY_NPAGE) else -1)
        elif key == "u":
            lib.filter(unread=not lib.unread)
        elif key == "g":
            lib.filter(starred=not lib.starred)
        elif key == "/":
            query = self.prompt("Buscar FTS5 (vacío limpia; Esc cancela)")
            if query is not None:
                lib.filter(query=query)
        elif key == "R":
            feed = repo.get_feed(self.conn, lib.feed_id) if lib.feed_id else None
            self.message = self.busy("Descargando novedades…", reader.refresh(
                self.conn, self.cfg, feed=feed, force=True,
            ))
            self.reload()
        elif key == "S":
            stats = self.busy("Sincronizando…", reader.sync(self.conn, self.cfg))
            self.message = f"Sincronizado: {stats}"
            self.reload()
        elif key == "a":
            url = self.prompt("URL de la fuente (Esc cancela)")
            if url:
                title = self.busy("Añadiendo fuente…", reader.subscribe(
                    self.conn, self.cfg, url,
                ))
                self.message = f"Añadido: {title}"
                self.reload()
        return True

    def loop(self):
        self.screen.keypad(True)
        with suppress(curses.error):
            curses.curs_set(0)
        while True:
            self.draw()
            try:
                if not self.handle(self.screen.get_wch()):
                    return
            except (ValueError, sqlite3.Error, httpx.HTTPError, OSError) as exc:
                self.message = f"Error: {exc}"


def run(conn, cfg: Config) -> int:
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda screen: TerminalReader(screen, conn, cfg).loop())
    except curses.error as exc:
        raise ValueError(f"No se pudo iniciar el terminal ({exc}); revisa TERM") from exc
    return 0
