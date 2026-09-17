"""Altas paginadas y recuperación de artículos que entran en el ámbito."""

from __future__ import annotations

import sqlite3

from .. import repo
from ..models import ChangeOp, Entity, Entry, SyncScope
from .apply import _FIELDS
from .scope import _scope_feed_ids, is_entry_in_scope


def arrival_cursor(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM entry_arrivals").fetchone()[0]


def entry_delta(
    conn: sqlite3.Connection, scope: SyncScope, since: int, limit: int,
    ops: list[ChangeOp],
) -> dict:
    rows = conn.execute(
        "SELECT seq, entry_id FROM entry_arrivals WHERE seq > ? ORDER BY seq LIMIT ?",
        (since, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = rows[-1]["seq"] if rows else since
    ids = {r["entry_id"] for r in rows}
    # Una estrella puede incorporar un artículo de hace años: no es un alta.
    ids.update(
        op.entity_id.partition(":")[0] for op in ops
        if op.entity in {Entity.ENTRY_STATE, Entity.ENTRY_TAG}
    )
    feeds = _scope_feed_ids(conn, scope)
    entries: list[Entry] = []
    dependencies: list[ChangeOp] = []
    states: list[ChangeOp] = []
    seen: set[tuple[Entity, str]] = set()

    def fields(entity: Entity, ident: str, row) -> list[ChangeOp]:
        result = []
        for field in _FIELDS[entity]:
            clock = repo.field_clock(conn, entity, ident, field) or (0, "")
            timestamp = row.get("updated_at") or 0
            if entity == Entity.ENTRY_STATE:
                timestamp = row.get("read_at" if field == "read" else "star_at") or 0
            result.append(ChangeOp(
                entity=entity, entity_id=ident, field=field, value=row[field],
                lamport=clock[0], device_id=clock[1], ts=timestamp,
            ))
        return result

    def dependency(entity: Entity, ident: str, table: str) -> None:
        key = (entity, ident)
        if key in seen:
            return
        seen.add(key)
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (ident,)).fetchone()
        if row is None:
            return
        data = dict(row)
        parent = data.get("parent_id") or data.get("folder_id")
        if parent:
            dependency(Entity.FOLDER, parent, "folders")
        dependencies.extend(fields(entity, ident, data))

    for ident in sorted(ids):
        if not is_entry_in_scope(conn, ident, scope, feed_ids=feeds):
            continue
        entry = repo.get_entry(conn, ident, with_body=False)
        if entry is None:
            continue
        entries.append(entry)
        dependency(Entity.FEED, entry.feed_id, "feeds")
        state = conn.execute("SELECT * FROM entry_state WHERE entry_id = ?", (ident,)).fetchone()
        if state:
            states.extend(fields(Entity.ENTRY_STATE, ident, dict(state)))
        for tag in conn.execute("SELECT * FROM entry_tags WHERE entry_id = ?", (ident,)):
            dependency(Entity.TAG, tag["tag_id"], "tags")
            states.extend(fields(Entity.ENTRY_TAG, f"{ident}:{tag['tag_id']}", dict(tag)))
    return {
        "entries": entries, "dependencies": dependencies, "entry_ops": states,
        "entries_cursor": cursor, "entries_has_more": more,
    }
