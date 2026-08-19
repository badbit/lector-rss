"""Sincronización: diario de cambios, snapshot de arranque y flujo SSE.

Este router es la única forma que tienen los clientes de compartir estado. No
sirve HTML: el hub no tiene interfaz web.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from rsscore import repo
from rsscore.ids import now_ms
from rsscore.models import PullResponse, PushRequest, PushResponse, SyncScope
from sse_starlette.sse import EventSourceResponse

from ..deps import bus, db, require_token, write_tx

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(require_token)])


class RegisterRequest(BaseModel):
    device_id: str
    name: str = ""
    scope: SyncScope = SyncScope()


@router.post("/register")
def register(req: RegisterRequest) -> dict:
    """Un cliente declara su identidad y qué parte del archivo replica."""
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO sync_clients (device_id, name, last_seq, scope_json, last_seen_at) "
            "VALUES (?, ?, 0, ?, ?) ON CONFLICT(device_id) DO UPDATE SET "
            "name = excluded.name, scope_json = excluded.scope_json, "
            "last_seen_at = excluded.last_seen_at",
            (req.device_id, req.name, req.scope.model_dump_json(), now_ms()),
        )
    return {"ok": True, "device_id": req.device_id}


def _client_scope(conn, device_id: str) -> SyncScope | None:
    row = conn.execute(
        "SELECT scope_json FROM sync_clients WHERE device_id = ?", (device_id,)
    ).fetchone()
    if not row:
        return None
    try:
        return SyncScope.model_validate_json(row["scope_json"])
    except Exception:
        return None


@router.get("/pull")
def pull(
    since: int = 0,
    limit: int = Query(default=2000, le=10000),
    device_id: str = "",
) -> PullResponse:
    conn = db()
    ops, cursor, has_more = repo.changes_since(conn, since, limit)

    # No devolvemos al emisor sus propias operaciones: ya las tiene aplicadas.
    if device_id:
        ops = [op for op in ops if op.device_id != device_id]
        scope = _client_scope(conn, device_id)
        if scope:
            from rsscore.sync import filter_ops_for_scope

            ops = filter_ops_for_scope(conn, ops, scope)

    if device_id:
        with write_tx() as c:
            c.execute(
                "UPDATE sync_clients SET last_seq = ?, last_seen_at = ? WHERE device_id = ?",
                (cursor, now_ms(), device_id),
            )
    lamport = conn.execute("SELECT lamport FROM node WHERE id = 1").fetchone()["lamport"]
    return PullResponse(ops=ops, cursor=cursor, has_more=has_more, server_lamport=lamport)


@router.post("/push")
def push(req: PushRequest) -> PushResponse:
    from rsscore.sync import apply_ops

    with write_tx() as conn:
        result = apply_ops(conn, req.ops, record=True)
        conn.execute(
            "INSERT INTO sync_clients (device_id, name, last_seq, scope_json, last_seen_at) "
            "VALUES (?, '', 0, '{}', ?) ON CONFLICT(device_id) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at",
            (req.device_id, now_ms()),
        )
        cursor = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM change_log").fetchone()["s"]
        lamport = conn.execute("SELECT lamport FROM node WHERE id = 1").fetchone()["lamport"]

    applied = getattr(result, "applied", 0)
    ignored = getattr(result, "ignored", 0)
    if applied:
        bus.publish({"type": "sync", "cursor": cursor})
    return PushResponse(accepted=applied, rejected=ignored, cursor=cursor, server_lamport=lamport)


@router.get("/snapshot")
def snapshot(device_id: str = "", days: int | None = None) -> dict:
    """Arranque de un cliente nuevo: el archivo es demasiado grande para
    reproducir el diario entero."""
    from rsscore.sync import build_snapshot

    conn = db()
    scope = _client_scope(conn, device_id) or SyncScope()
    if days is not None:
        scope.days = days
    return build_snapshot(conn, scope)


@router.get("/stream")
async def stream(request: Request, device_id: str = "") -> EventSourceResponse:
    """SSE: avisa a los clientes conectados de que hay novedades que traerse."""
    queue = bus.subscribe()

    async def gen():
        try:
            yield {"event": "hello", "data": json.dumps({"ok": True})}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}  # mantiene viva la conexión
                    continue
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(gen())


@router.get("/status")
def status() -> dict:
    conn = db()
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM change_log").fetchone()
    clients = conn.execute(
        "SELECT device_id, name, last_seq, last_seen_at FROM sync_clients "
        "ORDER BY last_seen_at DESC"
    ).fetchall()
    return {
        "cursor": row["s"],
        "clientes": [dict(c) for c in clients],
        "entradas": conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"],
        "feeds": conn.execute("SELECT COUNT(*) AS n FROM feeds WHERE deleted = 0").fetchone()["n"],
    }
