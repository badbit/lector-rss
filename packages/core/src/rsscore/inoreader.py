"""Importación transaccional del ZIP de Inoreader, sin descargar recursos."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from . import repo
from .db import device_id, tick_lamport
from .ids import canonical_url, hash_content, hash_guid, now_ms
from .models import Entity, Entry
from .opml import import_opml
from .parse import sanitize_html
from .reader import plain_html
from .rules.smart import SavedSearch, SavedSearchFilter, list_saved_searches, save_saved_search

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


class Item(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    title: str = ""
    author: str = ""
    published: int = Field(ge=0, le=253402300799)
    updated: int = Field(default=0, ge=0, le=253402300799)
    starred: int = Field(default=0, ge=0, le=253402300799)
    crawlTimeMsec: int = Field(default=0, ge=0, le=253402300799000)
    categories: list[str] = Field(default_factory=list)
    origin: dict
    summary: dict = Field(default_factory=dict)
    canonical: list[dict] = Field(default_factory=list)
    alternate: list[dict] = Field(default_factory=list)
    enclosure: list[dict] = Field(default_factory=list)


@dataclass
class Archive:
    sha256: str
    filename: str
    source: str
    xml: bytes
    metadata: dict
    items: list[tuple[Item, dict]]
    folders: dict[str, list[str]]


@dataclass
class ImportReport:
    feeds_new: int = 0
    feeds_existing: int = 0
    folders_new: int = 0
    entries_new: int = 0
    entries_linked: int = 0
    entries_skipped: int = 0
    views_created: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def _url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("La URL debe ser texto")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"URL no compatible: {value}")
    return value


def _folders(xml: bytes) -> dict[str, list[str]]:
    root = ET.fromstring(xml)
    if root.tag != "opml" or root.find("body") is None:
        raise ValueError("subscriptions.xml no es un OPML válido")
    folders: dict[str, list[str]] = {}
    names: dict[str, tuple] = {}

    def walk(node, path=()):
        for child in node.findall("outline"):
            url = child.get("xmlUrl") or child.get("xmlurl")
            title = (child.get("title") or child.get("text") or "").strip()
            if url:
                _url(url.strip())
                if path:
                    folders[path[-1]].append(url.strip())
                walk(child, path)
            elif title:
                target = (*path, title)
                if title in names and names[title] != target:
                    raise ValueError(f"Carpetas homónimas en rutas distintas: {title}")
                names[title] = target
                folders.setdefault(title, [])
                walk(child, target)
            else:
                walk(child, path)

    walk(root.find("body"))
    return folders


def read_archive(path: Path | str) -> Archive:
    path = Path(path).expanduser()
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("El ZIP supera el límite de 128 MiB")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)):
                raise ValueError("El ZIP contiene nombres repetidos")
            if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError("El ZIP descomprimido supera 128 MiB")
            if any(i.flag_bits & 1 for i in infos):
                raise ValueError("El ZIP está cifrado")
            if any(n.startswith("/") or ".." in Path(n).parts for n in names):
                raise ValueError("El ZIP contiene rutas no válidas")
            if not {"subscriptions.xml", "starred.json"}.issubset(names):
                raise ValueError("Faltan subscriptions.xml o starred.json")
            unknown = set(names) - {"subscriptions.xml", "starred.json", "README.txt"}
            if unknown:
                raise ValueError(f"Archivos adicionales aún no soportados: {sorted(unknown)}")
            if archive.testzip():
                raise ValueError("El ZIP no supera la comprobación CRC")
            xml = archive.read("subscriptions.xml")
            raw = json.loads(archive.read("starred.json"))
            readme = archive.read("README.txt").decode("utf-8") if "README.txt" in names else ""
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"Exportación ilegible: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("starred.json debe contener una lista items")
    try:
        folders = _folders(xml)
    except ET.ParseError as exc:
        raise ValueError(f"OPML ilegible: {exc}") from exc
    items, ids = [], set()
    for data in raw["items"]:
        item = Item.model_validate(data)
        if item.id in ids:
            raise ValueError(f"Identificador de artículo repetido: {item.id}")
        ids.add(item.id)
        _url(str(item.origin.get("streamId", "")).removeprefix("feed/"))
        if not isinstance(item.summary.get("content", ""), str):
            raise ValueError(f"Contenido no textual: {item.id}")
        for link in item.canonical + item.alternate + item.enclosure:
            if link.get("href"):
                _url(link["href"])
        items.append((item, data))
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return Archive(
        digest, path.name,
        "inoreader:" + str(raw.get("id", "starred")), xml,
        {**{k: v for k, v in raw.items() if k != "items"}, "readme": readme}, items, folders,
    )


def _set_imported_state(conn, entry_id: str, flag: str, value: bool, timestamp: int) -> None:
    """Conserva la fecha del estado también en el diario que reciben los clientes."""
    column = "star_at" if flag == "starred" else "read_at"
    lamport, dev = tick_lamport(conn), device_id(conn)
    conn.execute(
        f"UPDATE entry_state SET {flag}=?, {column}=?, lamport=?, device_id=? WHERE entry_id=?",
        (int(value), timestamp or None, lamport, dev, entry_id),
    )
    repo.append_change(conn, Entity.ENTRY_STATE, entry_id, flag, value,
                       lamport=lamport, ts=timestamp)


def _import_item(conn, archive: Archive, item: Item, raw: dict, report: ImportReport):
    known = conn.execute(
        "SELECT entry_id FROM import_records WHERE source=? AND external_id=?",
        (archive.source, item.id),
    ).fetchone()
    if known:
        report.entries_skipped += 1
        return
    feed_url = item.origin["streamId"].removeprefix("feed/")
    feed = repo.feed_by_url(conn, feed_url)
    if feed is None:
        from .models import Feed

        # Una fuente ya cancelada puede seguir teniendo artículos guardados.
        feed = repo.add_feed(conn, Feed(url=feed_url, title=item.origin.get("title") or feed_url,
                                       site_url=item.origin.get("htmlUrl"), disabled=True))
        repo.append_change(conn, Entity.FEED, feed.id, "disabled", True)
        report.feeds_new += 1
        report.warnings.append(f"Fuente archivada, sin refresco automático: {feed.display_title}")
    if feed.deleted:
        raise ValueError(f"La fuente está eliminada en el destino: {feed.display_title}")
    links = item.canonical or item.alternate
    url = links[0].get("href") if links else None
    html = sanitize_html(item.summary.get("content", ""), base_url=url or feed_url)
    text = plain_html(html)
    enclosure = item.enclosure[0] if item.enclosure else {}
    entry = Entry(
        feed_id=feed.id, guid_hash=hash_guid(feed.id, item.id),
        content_hash=hash_content(item.title, text), url=url, title=item.title,
        author=item.author or None, summary=text[:500], published_at=item.published * 1000,
        updated_at=item.updated * 1000 or None,
        fetched_at=item.crawlTimeMsec or item.published * 1000,
        body_html=html or None, body_text=text or None,
        enclosure_url=enclosure.get("href"), enclosure_type=enclosure.get("type"),
    )
    matches = [row["id"] for row in conn.execute(
        "SELECT id, url, guid_hash FROM entries WHERE feed_id=? AND "
        "(guid_hash=? OR published_at BETWEEN ? AND ?)",
        (feed.id, entry.guid_hash, entry.published_at - repo.DUPLICATE_WINDOW_MS,
         entry.published_at + repo.DUPLICATE_WINDOW_MS),
    ) if row["guid_hash"] == entry.guid_hash or (
        url and canonical_url(row["url"]) == canonical_url(url)
    )]
    if len(matches) > 1:
        report.entries_skipped += 1
        report.warnings.append(f"Coincidencia ambigua, artículo pendiente: {item.id}")
        return
    new = not matches
    if new:
        repo.insert_entry(conn, entry, track=True)
        report.entries_new += 1
    else:
        entry.id = matches[0]
        report.entries_linked += 1
        # Mantiene el cuerpo existente; rellena únicamente uno ausente.
        if not any(repo.get_body(conn, entry.id)) and (html or text):
            repo.update_entry_body(conn, entry.id, html=html, text=text)
    starred = bool(item.starred) or any(c.endswith("/state/com.google/starred")
                                      for c in item.categories)
    read = any(c.endswith("/state/com.google/read") for c in item.categories)
    state = repo.get_state(conn, entry.id)
    if new:
        # La exportación no aporta la fecha de lectura; no se inventa una.
        _set_imported_state(conn, entry.id, "read", read, 0)
    if starred and (new or not state.starred):
        _set_imported_state(conn, entry.id, "starred", True, item.starred * 1000)
    for category in item.categories:
        if "/label/" in category:
            label = category.split("/label/", 1)[1]
            if label and label not in archive.folders:
                tag = repo.get_or_create_tag(conn, label)
                repo.tag_entry(conn, entry.id, tag.id)
    conn.execute(
        "INSERT INTO import_records VALUES (?,?,?,?,?)",
        (archive.source, item.id, entry.id, json.dumps(raw, ensure_ascii=False), now_ms()),
    )


def import_archive(conn: sqlite3.Connection, archive: Archive) -> ImportReport:
    report = ImportReport()
    # SAVEPOINT permite usar el importador dentro de la transacción del hub.
    conn.execute("SAVEPOINT inoreader_import")
    try:
        opml = import_opml(conn, archive.xml)
        if opml.errores:
            raise ValueError("; ".join(opml.errores))
        report.feeds_new, report.feeds_existing = opml.feeds_nuevos, opml.feeds_repetidos
        report.folders_new = opml.carpetas_nuevas
        for item, raw in archive.items:
            _import_item(conn, archive, item, raw, report)
        saved = {s.id: s for s in list_saved_searches(conn)}
        for name, urls in archive.folders.items():
            folder = repo.folder_by_name(conn, name)
            extras = [f.id for url in urls if (f := repo.feed_by_url(conn, url))
                      and f.folder_id != folder.id]
            if extras:
                key = hashlib.sha256(f"{archive.source}:{name}".encode()).hexdigest()[:24]
                view = SavedSearch(
                    id=f"inoreader-{key}", name=f"{name} · Inoreader",
                    filter=SavedSearchFilter(folder_ids=[folder.id], feed_ids=extras),
                )
                if saved.get(view.id) != view:
                    save_saved_search(conn, view)
                    report.views_created += 1
        conn.execute(
            "INSERT OR IGNORE INTO import_batches VALUES (?,?,?,?,?,?,?)",
            (archive.sha256, archive.source, archive.filename, now_ms(),
             archive.xml.decode("utf-8"), json.dumps(archive.metadata, ensure_ascii=False),
             json.dumps(report.as_dict(), ensure_ascii=False)),
        )
        conn.execute("RELEASE inoreader_import")
    except BaseException:
        conn.execute("ROLLBACK TO inoreader_import")
        conn.execute("RELEASE inoreader_import")
        raise
    return report
