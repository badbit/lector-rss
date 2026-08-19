"""Importación y exportación OPML.

OPML es la única vía real para entrar y salir de un lector de feeds; sin esto el
archivo queda secuestrado. Se admiten los dos dialectos que existen de hecho:
carpetas como `<outline>` anidados (Liferea, FreshRSS, Feedly) y ficheros planos.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.dom import minidom

from . import repo
from .models import Feed, Folder

__all__ = ["ImportResult", "export_opml", "import_opml"]


@dataclass(slots=True)
class ImportResult:
    feeds_nuevos: int = 0
    feeds_repetidos: int = 0
    carpetas_nuevas: int = 0
    errores: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "importados": self.feeds_nuevos,
            "repetidos": self.feeds_repetidos,
            "carpetas": self.carpetas_nuevas,
            "errores": self.errores,
        }

    def __str__(self) -> str:
        return (
            f"{self.feeds_nuevos} feeds nuevos, {self.feeds_repetidos} ya estaban, "
            f"{self.carpetas_nuevas} carpetas"
        )


def import_opml(conn: sqlite3.Connection, xml: str | bytes) -> ImportResult:
    """Carga suscripciones desde OPML, creando las carpetas que hagan falta."""
    result = ImportResult()
    try:
        raiz = ET.fromstring(xml if isinstance(xml, str) else xml.decode("utf-8", "replace"))
    except ET.ParseError as exc:
        result.errores.append(f"OPML ilegible: {exc}")
        return result

    body = raiz.find("body")
    if body is None:
        result.errores.append("el OPML no tiene <body>")
        return result

    _walk(conn, body, None, result)
    return result


def _walk(
    conn: sqlite3.Connection, nodo: ET.Element, folder_id: str | None, result: ImportResult
) -> None:
    for outline in nodo.findall("outline"):
        url = outline.get("xmlUrl") or outline.get("xmlurl")
        titulo = (outline.get("title") or outline.get("text") or "").strip()

        if url:
            _add_feed(conn, url.strip(), titulo, outline, folder_id, result)
            # Algunos exportadores anidan feeds dentro de un feed; se respeta.
            _walk(conn, outline, folder_id, result)
            continue

        if not titulo:
            _walk(conn, outline, folder_id, result)  # outline decorativo
            continue

        carpeta = repo.folder_by_name(conn, titulo)
        if carpeta is None:
            carpeta = repo.upsert_folder(conn, Folder(name=titulo, parent_id=folder_id))
            result.carpetas_nuevas += 1
        _walk(conn, outline, carpeta.id, result)


def _add_feed(
    conn: sqlite3.Connection,
    url: str,
    titulo: str,
    outline: ET.Element,
    folder_id: str | None,
    result: ImportResult,
) -> None:
    if repo.feed_by_url(conn, url) is not None:
        result.feeds_repetidos += 1
        return
    feed = Feed(
        url=url,
        title=titulo or url,
        site_url=outline.get("htmlUrl") or outline.get("htmlurl"),
        description=outline.get("description") or None,
        folder_id=folder_id,
    )
    repo.add_feed(conn, feed)
    result.feeds_nuevos += 1


# ------------------------------------------------------------------ exportación
def export_opml(conn: sqlite3.Connection, *, title: str = "Suscripciones RSS") -> str:
    """Genera un OPML 2.0 con la jerarquía de carpetas completa."""
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    body = ET.SubElement(opml, "body")

    carpetas = repo.list_folders(conn)
    feeds = repo.list_feeds(conn)
    por_carpeta: dict[str | None, list[Feed]] = {}
    for f in feeds:
        por_carpeta.setdefault(f.folder_id, []).append(f)

    hijas: dict[str | None, list[Folder]] = {}
    for c in carpetas:
        hijas.setdefault(c.parent_id, []).append(c)

    def emitir_carpeta(carpeta: Folder, padre: ET.Element) -> None:
        nodo = ET.SubElement(padre, "outline", text=carpeta.name, title=carpeta.name)
        for hija in hijas.get(carpeta.id, []):
            emitir_carpeta(hija, nodo)
        for feed in por_carpeta.get(carpeta.id, []):
            emitir_feed(feed, nodo)

    def emitir_feed(feed: Feed, padre: ET.Element) -> None:
        attrs = {
            "type": "rss",
            "text": feed.display_title,
            "title": feed.display_title,
            "xmlUrl": feed.url,
        }
        if feed.site_url:
            attrs["htmlUrl"] = feed.site_url
        if feed.description:
            attrs["description"] = feed.description
        ET.SubElement(padre, "outline", **attrs)

    for carpeta in hijas.get(None, []):
        emitir_carpeta(carpeta, body)
    for feed in por_carpeta.get(None, []):
        emitir_feed(feed, body)

    crudo = ET.tostring(opml, encoding="unicode")
    return minidom.parseString(crudo).toprettyxml(indent="  ")
