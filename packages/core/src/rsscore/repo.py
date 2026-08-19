"""Capa de acceso a datos.

Es el contrato que comparten ingesta, sincronización, reglas, exportadores, hub y
clientes: todo el SQL vive aquí y en ningún otro sitio. Las funciones reciben la
conexión explícitamente para que quien llama controle la transacción.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from typing import Any

from .db import compress, decompress, device_id, tick_lamport
from .ids import new_id, now_ms
from .models import (
    ChangeOp,
    Entity,
    Entry,
    EntrySelection,
    EntryState,
    ExportJob,
    ExportKind,
    ExportStatus,
    Feed,
    Folder,
    Tag,
)

# ============================================================ diario de cambios


def append_change(
    conn: sqlite3.Connection,
    entity: Entity,
    entity_id: str,
    field: str,
    value: Any,
    *,
    lamport: int | None = None,
    dev: str | None = None,
    to_outbox: bool = True,
) -> ChangeOp:
    """Registra una escritura local en el diario (y en la cola de subida)."""
    dev = dev or device_id(conn)
    lamport = tick_lamport(conn) if lamport is None else lamport
    op = ChangeOp(
        device_id=dev,
        lamport=lamport,
        entity=entity,
        entity_id=entity_id,
        field=field,
        value=value,
        ts=now_ms(),
    )
    value_json = json.dumps(value, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO change_log (device_id, lamport, entity, entity_id, field, value_json, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (op.device_id, op.lamport, str(op.entity), op.entity_id, op.field, value_json, op.ts),
    )
    op.seq = cur.lastrowid
    # El reloj se guarda por campo: es lo que permite resolver el conflicto de
    # cada campo por separado y que el orden de llegada deje de importar.
    set_field_clock(conn, entity, entity_id, field, lamport, dev)
    if to_outbox:
        conn.execute(
            "INSERT INTO outbox (device_id, lamport, entity, entity_id, field, value_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (op.device_id, op.lamport, str(op.entity), op.entity_id, op.field, value_json, op.ts),
        )
    return op


def field_clock(
    conn: sqlite3.Connection, entity: Entity, entity_id: str, field: str
) -> tuple[int, str] | None:
    """Reloj `(lamport, device_id)` de un campo concreto, o None si nunca se tocó."""
    row = conn.execute(
        "SELECT lamport, device_id FROM field_clock WHERE entity = ? AND entity_id = ? "
        "AND field = ?",
        (str(entity), entity_id, field),
    ).fetchone()
    return (row["lamport"], row["device_id"]) if row else None


def set_field_clock(
    conn: sqlite3.Connection,
    entity: Entity,
    entity_id: str,
    field: str,
    lamport: int,
    dev: str,
) -> None:
    conn.execute(
        "INSERT INTO field_clock (entity, entity_id, field, lamport, device_id) "
        "VALUES (?,?,?,?,?) ON CONFLICT(entity, entity_id, field) DO UPDATE SET "
        "lamport = excluded.lamport, device_id = excluded.device_id",
        (str(entity), entity_id, field, lamport, dev),
    )


def changes_since(
    conn: sqlite3.Connection, cursor: int, limit: int = 2000
) -> tuple[list[ChangeOp], int, bool]:
    """Lee el diario a partir de un cursor. Devuelve (ops, nuevo_cursor, hay_más)."""
    rows = conn.execute(
        "SELECT * FROM change_log WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, limit + 1)
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    ops = [_row_to_op(r) for r in rows]
    new_cursor = ops[-1].seq if ops else cursor
    return ops, int(new_cursor or cursor), has_more


def _row_to_op(row: sqlite3.Row) -> ChangeOp:
    return ChangeOp(
        seq=row["seq"],
        device_id=row["device_id"],
        lamport=row["lamport"],
        entity=Entity(row["entity"]),
        entity_id=row["entity_id"],
        field=row["field"],
        value=json.loads(row["value_json"]),
        ts=row["ts"],
    )


def outbox_batch(conn: sqlite3.Connection, limit: int = 500) -> list[tuple[int, ChangeOp]]:
    rows = conn.execute("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [
        (
            r["id"],
            ChangeOp(
                device_id=r["device_id"],
                lamport=r["lamport"],
                entity=Entity(r["entity"]),
                entity_id=r["entity_id"],
                field=r["field"],
                value=json.loads(r["value_json"]),
                ts=r["ts"],
            ),
        )
        for r in rows
    ]


def outbox_clear(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    ids = list(ids)
    if not ids:
        return
    conn.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])


# ======================================================================= folders


def upsert_folder(conn: sqlite3.Connection, folder: Folder, *, track: bool = True) -> Folder:
    conn.execute(
        "INSERT INTO folders (id, parent_id, name, position, deleted, lamport, device_id, "
        "updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, name=excluded.name, "
        "position=excluded.position, deleted=excluded.deleted, lamport=excluded.lamport, "
        "device_id=excluded.device_id, updated_at=excluded.updated_at",
        (
            folder.id,
            folder.parent_id,
            folder.name,
            folder.position,
            int(folder.deleted),
            folder.lamport,
            folder.device_id,
            folder.updated_at,
        ),
    )
    if track:
        append_change(conn, Entity.FOLDER, folder.id, "name", folder.name)
        append_change(conn, Entity.FOLDER, folder.id, "parent_id", folder.parent_id)
    return folder


def list_folders(conn: sqlite3.Connection, *, include_deleted: bool = False) -> list[Folder]:
    sql = "SELECT * FROM folders"
    if not include_deleted:
        sql += " WHERE deleted = 0"
    sql += " ORDER BY position, name"
    return [_folder(r) for r in conn.execute(sql)]


def get_folder(conn: sqlite3.Connection, folder_id: str) -> Folder | None:
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return _folder(row) if row else None


def folder_by_name(conn: sqlite3.Connection, name: str) -> Folder | None:
    row = conn.execute("SELECT * FROM folders WHERE name = ? AND deleted = 0", (name,)).fetchone()
    return _folder(row) if row else None


def descendant_folder_ids(conn: sqlite3.Connection, folder_ids: Iterable[str]) -> list[str]:
    """Expande carpetas a toda su descendencia (las carpetas anidan)."""
    out: list[str] = []
    pending = list(folder_ids)
    seen: set[str] = set()
    while pending:
        fid = pending.pop()
        if fid in seen:
            continue
        seen.add(fid)
        out.append(fid)
        for r in conn.execute("SELECT id FROM folders WHERE parent_id = ? AND deleted = 0", (fid,)):
            pending.append(r["id"])
    return out


def _folder(row: sqlite3.Row) -> Folder:
    return Folder(
        id=row["id"],
        parent_id=row["parent_id"],
        name=row["name"],
        position=row["position"],
        deleted=bool(row["deleted"]),
        lamport=row["lamport"],
        device_id=row["device_id"],
        updated_at=row["updated_at"],
    )


# ========================================================================= feeds

_FEED_COLS = (
    "id, folder_id, url, site_url, title, custom_title, description, icon_url, etag, "
    "last_modified, last_fetch_at, last_success_at, next_fetch_at, interval_seconds, "
    "error_count, last_error, disabled, fetch_full_text, keep_unread, source_kind, "
    "source_config_json, watch_hash, deleted, lamport, device_id, updated_at"
)
# Derivado de la lista, no escrito a mano: al añadir una columna el número de
# marcadores se ajusta solo.
_FEED_PLACEHOLDERS = ",".join("?" * len(_FEED_COLS.split(",")))


def add_feed(conn: sqlite3.Connection, feed: Feed, *, track: bool = True) -> Feed:
    existing = feed_by_url(conn, feed.url)
    if existing:
        return existing
    conn.execute(
        f"INSERT INTO feeds ({_FEED_COLS}) VALUES ({_FEED_PLACEHOLDERS})",
        (
            feed.id,
            feed.folder_id,
            feed.url,
            feed.site_url,
            feed.title,
            feed.custom_title,
            feed.description,
            feed.icon_url,
            feed.etag,
            feed.last_modified,
            feed.last_fetch_at,
            feed.last_success_at,
            feed.next_fetch_at,
            feed.interval_seconds,
            feed.error_count,
            feed.last_error,
            int(feed.disabled),
            int(feed.fetch_full_text),
            int(feed.keep_unread),
            feed.source_kind,
            feed.source_config_json,
            feed.watch_hash,
            int(feed.deleted),
            feed.lamport,
            feed.device_id,
            feed.updated_at,
        ),
    )
    if track:
        append_change(conn, Entity.FEED, feed.id, "url", feed.url)
        append_change(conn, Entity.FEED, feed.id, "title", feed.title)
        append_change(conn, Entity.FEED, feed.id, "folder_id", feed.folder_id)
    return feed


def get_feed(conn: sqlite3.Connection, feed_id: str) -> Feed | None:
    row = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    return _feed(row) if row else None


def feed_by_url(conn: sqlite3.Connection, url: str) -> Feed | None:
    row = conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
    return _feed(row) if row else None


def list_feeds(
    conn: sqlite3.Connection, *, folder_id: str | None = None, include_deleted: bool = False
) -> list[Feed]:
    sql, params = "SELECT * FROM feeds WHERE 1=1", []
    if not include_deleted:
        sql += " AND deleted = 0"
    if folder_id is not None:
        sql += " AND folder_id = ?"
        params.append(folder_id)
    sql += " ORDER BY COALESCE(custom_title, title), url"
    return [_feed(r) for r in conn.execute(sql, params)]


def due_feeds(conn: sqlite3.Connection, now: int | None = None, limit: int = 1000) -> list[Feed]:
    """Feeds que toca refrescar ahora."""
    now = now or now_ms()
    rows = conn.execute(
        "SELECT * FROM feeds WHERE disabled = 0 AND deleted = 0 AND next_fetch_at <= ? "
        "ORDER BY next_fetch_at LIMIT ?",
        (now, limit),
    ).fetchall()
    return [_feed(r) for r in rows]


def update_feed_meta(conn: sqlite3.Connection, feed_id: str, **fields: Any) -> None:
    """Actualiza campos de contabilidad de descarga. No genera cambios sincronizables."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE feeds SET {cols} WHERE id = ?", (*fields.values(), feed_id))


def set_feed_folder(conn: sqlite3.Connection, feed_id: str, folder_id: str | None) -> None:
    lam = tick_lamport(conn)
    conn.execute(
        "UPDATE feeds SET folder_id = ?, lamport = ?, device_id = ?, updated_at = ? WHERE id = ?",
        (folder_id, lam, device_id(conn), now_ms(), feed_id),
    )
    append_change(conn, Entity.FEED, feed_id, "folder_id", folder_id, lamport=lam)


def delete_feed(conn: sqlite3.Connection, feed_id: str) -> None:
    lam = tick_lamport(conn)
    conn.execute(
        "UPDATE feeds SET deleted = 1, lamport = ?, device_id = ?, updated_at = ? WHERE id = ?",
        (lam, device_id(conn), now_ms(), feed_id),
    )
    append_change(conn, Entity.FEED, feed_id, "deleted", True, lamport=lam)


def _feed(row: sqlite3.Row) -> Feed:
    d = dict(row)
    for b in ("disabled", "fetch_full_text", "keep_unread", "deleted"):
        d[b] = bool(d[b])
    return Feed(**d)


# ======================================================================= entradas


def entry_exists(conn: sqlite3.Connection, feed_id: str, guid_hash: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM entries WHERE feed_id = ? AND guid_hash = ?", (feed_id, guid_hash)
    ).fetchone()
    return row["id"] if row else None


def content_hash_exists(conn: sqlite3.Connection, content_hash: str) -> str | None:
    """Duplicado entre feeds distintos (la misma noticia sindicada dos veces)."""
    row = conn.execute(
        "SELECT id FROM entries WHERE content_hash = ? LIMIT 1", (content_hash,)
    ).fetchone()
    return row["id"] if row else None


def insert_entry(conn: sqlite3.Connection, entry: Entry) -> str:
    """Inserta la entrada, su cuerpo comprimido y su índice de búsqueda."""
    cur = conn.execute(
        "INSERT INTO entries (id, feed_id, guid_hash, content_hash, url, title, author, summary, "
        "published_at, updated_at, fetched_at, has_body, full_text_at, enclosure_url, "
        "enclosure_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry.id,
            entry.feed_id,
            entry.guid_hash,
            entry.content_hash,
            entry.url,
            entry.title,
            entry.author,
            entry.summary,
            entry.published_at,
            entry.updated_at,
            entry.fetched_at,
            int(bool(entry.body_html or entry.body_text)),
            entry.full_text_at,
            entry.enclosure_url,
            entry.enclosure_type,
        ),
    )
    rowid = cur.lastrowid
    if entry.body_html or entry.body_text:
        raw = len((entry.body_html or "").encode()) + len((entry.body_text or "").encode())
        conn.execute(
            "INSERT INTO entry_bodies (entry_id, html_zstd, text_zstd, bytes_raw) VALUES (?,?,?,?)",
            (entry.id, compress(entry.body_html), compress(entry.body_text), raw),
        )
    conn.execute(
        "INSERT INTO entry_state (entry_id, read, starred, lamport, device_id) VALUES (?,0,0,0,'')",
        (entry.id,),
    )
    conn.execute(
        "INSERT INTO entries_fts (rowid, title, author, body) VALUES (?,?,?,?)",
        (rowid, entry.title, entry.author or "", entry.body_text or entry.summary or ""),
    )
    return entry.id


def update_entry_body(
    conn: sqlite3.Connection, entry_id: str, *, html: str | None, text: str | None
) -> None:
    """Guarda el texto completo extraído y reindexa."""
    raw = len((html or "").encode()) + len((text or "").encode())
    conn.execute(
        "INSERT INTO entry_bodies (entry_id, html_zstd, text_zstd, bytes_raw) VALUES (?,?,?,?) "
        "ON CONFLICT(entry_id) DO UPDATE SET html_zstd=excluded.html_zstd, "
        "text_zstd=excluded.text_zstd, bytes_raw=excluded.bytes_raw",
        (entry_id, compress(html), compress(text), raw),
    )
    conn.execute(
        "UPDATE entries SET has_body = 1, full_text_at = ? WHERE id = ?", (now_ms(), entry_id)
    )
    row = conn.execute(
        "SELECT rowid, title, author FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (row["rowid"],))
        conn.execute(
            "INSERT INTO entries_fts (rowid, title, author, body) VALUES (?,?,?,?)",
            (row["rowid"], row["title"], row["author"] or "", text or ""),
        )


def get_entry(conn: sqlite3.Connection, entry_id: str, *, with_body: bool = True) -> Entry | None:
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        return None
    entry = _entry(row)
    if with_body and row["has_body"]:
        b = conn.execute(
            "SELECT html_zstd, text_zstd FROM entry_bodies WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if b:
            entry.body_html = decompress(b["html_zstd"])
            entry.body_text = decompress(b["text_zstd"])
    return entry


def get_body(conn: sqlite3.Connection, entry_id: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT html_zstd, text_zstd FROM entry_bodies WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    if not row:
        return None, None
    return decompress(row["html_zstd"]), decompress(row["text_zstd"])


def select_entries(conn: sqlite3.Connection, sel: EntrySelection) -> list[Entry]:
    """Resuelve una selección (la usan la UI, las revistas y las exportaciones)."""
    sql = [
        "SELECT e.* FROM entries e",
        "JOIN entry_state s ON s.entry_id = e.id",
    ]
    where, params = ["1=1"], []

    if sel.entry_ids:
        where.append(f"e.id IN ({','.join('?' * len(sel.entry_ids))})")
        params += sel.entry_ids
    feed_ids = list(sel.feed_ids)
    if sel.folder_ids:
        folders = descendant_folder_ids(conn, sel.folder_ids)
        rows = conn.execute(
            "SELECT id FROM feeds WHERE deleted = 0 AND folder_id IN "
            f"({','.join('?' * len(folders))})",
            folders,
        ).fetchall()
        feed_ids += [r["id"] for r in rows]
    if feed_ids:
        where.append(f"e.feed_id IN ({','.join('?' * len(feed_ids))})")
        params += feed_ids
    if sel.tag_ids:
        sql.append(
            "JOIN entry_tags et ON et.entry_id = e.id AND et.deleted = 0 "
            f"AND et.tag_id IN ({','.join('?' * len(sel.tag_ids))})"
        )
        params = sel.tag_ids + params  # el JOIN va antes que el WHERE
    if sel.query:
        sql.append("JOIN entries_fts f ON f.rowid = e.rowid")
        where.append("entries_fts MATCH ?")
        params.append(sel.query)
    if sel.unread_only:
        where.append("s.read = 0")
    if sel.starred_only:
        where.append("s.starred = 1")
    if sel.since:
        where.append("e.published_at >= ?")
        params.append(sel.since)
    if sel.until:
        where.append("e.published_at <= ?")
        params.append(sel.until)

    query = " ".join(sql) + " WHERE " + " AND ".join(where)
    query += " ORDER BY e.published_at DESC LIMIT ? OFFSET ?"
    params.append(sel.limit)
    params.append(sel.offset)
    return [_entry(r) for r in conn.execute(query, params)]


def iter_entries_with_body(conn: sqlite3.Connection, entries: list[Entry]) -> Iterator[Entry]:
    """Rellena los cuerpos de una lista ya seleccionada, uno a uno."""
    for entry in entries:
        entry.body_html, entry.body_text = get_body(conn, entry.id)
        yield entry


def search(conn: sqlite3.Connection, query: str, limit: int = 100) -> list[Entry]:
    rows = conn.execute(
        "SELECT e.* FROM entries_fts f JOIN entries e ON e.rowid = f.rowid "
        "WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()
    return [_entry(r) for r in rows]


def unread_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """No leídos por feed. Usa el índice parcial, no escanea el archivo."""
    rows = conn.execute(
        "SELECT e.feed_id AS fid, COUNT(*) AS n FROM entry_state s "
        "JOIN entries e ON e.id = s.entry_id WHERE s.read = 0 GROUP BY e.feed_id"
    ).fetchall()
    return {r["fid"]: r["n"] for r in rows}


def _entry(row: sqlite3.Row) -> Entry:
    d = {k: row[k] for k in row.keys() if k in Entry.model_fields}  # noqa: SIM118
    d["has_body"] = bool(d.get("has_body"))
    return Entry(**d)


# ========================================================================= estado


def get_state(conn: sqlite3.Connection, entry_id: str) -> EntryState | None:
    row = conn.execute("SELECT * FROM entry_state WHERE entry_id = ?", (entry_id,)).fetchone()
    if not row:
        return None
    return EntryState(
        entry_id=row["entry_id"],
        read=bool(row["read"]),
        starred=bool(row["starred"]),
        read_at=row["read_at"],
        star_at=row["star_at"],
        lamport=row["lamport"],
        device_id=row["device_id"],
    )


def set_read(conn: sqlite3.Connection, entry_ids: Iterable[str], read: bool = True) -> int:
    return _set_state_flag(conn, entry_ids, "read", read)


def set_starred(conn: sqlite3.Connection, entry_ids: Iterable[str], starred: bool = True) -> int:
    return _set_state_flag(conn, entry_ids, "starred", starred)


def _set_state_flag(
    conn: sqlite3.Connection, entry_ids: Iterable[str], field: str, value: bool
) -> int:
    ids = list(entry_ids)
    if not ids:
        return 0
    dev, ts = device_id(conn), now_ms()
    at_col = "read_at" if field == "read" else "star_at"
    for entry_id in ids:
        lam = tick_lamport(conn)
        conn.execute(
            f"INSERT INTO entry_state (entry_id, {field}, {at_col}, lamport, device_id) "
            f"VALUES (?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET {field}=excluded.{field}, "
            f"{at_col}=excluded.{at_col}, lamport=excluded.lamport, device_id=excluded.device_id",
            (entry_id, int(value), ts if value else None, lam, dev),
        )
        append_change(conn, Entity.ENTRY_STATE, entry_id, field, value, lamport=lam)
    return len(ids)


def mark_feed_read(conn: sqlite3.Connection, feed_id: str) -> int:
    rows = conn.execute(
        "SELECT s.entry_id FROM entry_state s JOIN entries e ON e.id = s.entry_id "
        "WHERE e.feed_id = ? AND s.read = 0",
        (feed_id,),
    ).fetchall()
    return set_read(conn, [r["entry_id"] for r in rows], True)


# ====================================================================== etiquetas


def get_or_create_tag(conn: sqlite3.Connection, name: str, color: str | None = None) -> Tag:
    row = conn.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return Tag(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            deleted=bool(row["deleted"]),
            lamport=row["lamport"],
            device_id=row["device_id"],
        )
    tag = Tag(
        id=new_id(), name=name, color=color, lamport=tick_lamport(conn), device_id=device_id(conn)
    )
    conn.execute(
        "INSERT INTO tags (id, name, color, deleted, lamport, device_id) VALUES (?,?,?,0,?,?)",
        (tag.id, tag.name, tag.color, tag.lamport, tag.device_id),
    )
    append_change(conn, Entity.TAG, tag.id, "name", tag.name, lamport=tag.lamport)
    return tag


def list_tags(conn: sqlite3.Connection) -> list[Tag]:
    rows = conn.execute("SELECT * FROM tags WHERE deleted = 0 ORDER BY name").fetchall()
    return [
        Tag(
            id=r["id"],
            name=r["name"],
            color=r["color"],
            deleted=False,
            lamport=r["lamport"],
            device_id=r["device_id"],
        )
        for r in rows
    ]


def tag_entry(
    conn: sqlite3.Connection, entry_id: str, tag_id: str, *, remove: bool = False
) -> None:
    """LWW-element-set: quitar una etiqueta deja tombstone, no borra la fila."""
    lam, dev = tick_lamport(conn), device_id(conn)
    conn.execute(
        "INSERT INTO entry_tags (entry_id, tag_id, deleted, lamport, device_id) VALUES (?,?,?,?,?) "
        "ON CONFLICT(entry_id, tag_id) DO UPDATE SET deleted=excluded.deleted, "
        "lamport=excluded.lamport, device_id=excluded.device_id",
        (entry_id, tag_id, int(remove), lam, dev),
    )
    append_change(conn, Entity.ENTRY_TAG, f"{entry_id}:{tag_id}", "deleted", remove, lamport=lam)


def entry_tags(conn: sqlite3.Connection, entry_id: str) -> list[Tag]:
    rows = conn.execute(
        "SELECT t.* FROM entry_tags et JOIN tags t ON t.id = et.tag_id "
        "WHERE et.entry_id = ? AND et.deleted = 0 AND t.deleted = 0 ORDER BY t.name",
        (entry_id,),
    ).fetchall()
    return [
        Tag(
            id=r["id"],
            name=r["name"],
            color=r["color"],
            deleted=False,
            lamport=r["lamport"],
            device_id=r["device_id"],
        )
        for r in rows
    ]


# =================================================================== exportación


def enqueue_export(conn: sqlite3.Connection, job: ExportJob) -> ExportJob:
    conn.execute(
        "INSERT INTO export_jobs (id, kind, status, target, params_json, result_json, error, "
        "created_at, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            job.id,
            str(job.kind),
            str(job.status),
            job.target,
            json.dumps(job.params, ensure_ascii=False),
            json.dumps(job.result, ensure_ascii=False),
            job.error,
            job.created_at,
            job.started_at,
            job.finished_at,
        ),
    )
    return job


def claim_export(conn: sqlite3.Connection, target: str) -> ExportJob | None:
    """Toma el siguiente trabajo pendiente para este destino, marcándolo en curso."""
    row = conn.execute(
        "SELECT * FROM export_jobs WHERE status = 'pending' AND target = ? "
        "ORDER BY created_at LIMIT 1",
        (target,),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE export_jobs SET status = 'running', started_at = ? WHERE id = ? "
        "AND status = 'pending'",
        (now_ms(), row["id"]),
    )
    return _job(row)


def finish_export(
    conn: sqlite3.Connection, job_id: str, *, result: dict | None = None, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE export_jobs SET status = ?, result_json = ?, error = ?, finished_at = ? "
        "WHERE id = ?",
        (
            str(ExportStatus.ERROR if error else ExportStatus.DONE),
            json.dumps(result or {}, ensure_ascii=False),
            error,
            now_ms(),
            job_id,
        ),
    )


def list_exports(conn: sqlite3.Connection, limit: int = 50) -> list[ExportJob]:
    rows = conn.execute(
        "SELECT * FROM export_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_job(r) for r in rows]


def _job(row: sqlite3.Row) -> ExportJob:
    return ExportJob(
        id=row["id"],
        kind=ExportKind(row["kind"]),
        status=ExportStatus(row["status"]),
        target=row["target"],
        params=json.loads(row["params_json"]),
        result=json.loads(row["result_json"]),
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
