"""Lectura de artículos, estado y búsqueda."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from rsscore import repo
from rsscore.models import EntrySelection

from ..deps import bus, db, require_token, write_tx

router = APIRouter(prefix="/entries", tags=["entries"], dependencies=[Depends(require_token)])


class EntryOut(BaseModel):
    id: str
    feed_id: str
    url: str | None
    title: str
    author: str | None
    summary: str | None
    published_at: int
    read: bool = False
    starred: bool = False
    tags: list[str] = []


class EntryDetail(EntryOut):
    body_html: str | None = None
    body_text: str | None = None


class StateRequest(BaseModel):
    entry_ids: list[str]
    value: bool = True


@router.get("")
def list_entries(
    feed_id: str | None = None,
    folder_id: str | None = None,
    tag_id: str | None = None,
    query: str | None = None,
    unread: bool = False,
    starred: bool = False,
    since: int | None = None,
    until: int | None = None,
    limit: int = Query(default=100, le=1000),
) -> list[EntryOut]:
    conn = db()
    sel = EntrySelection(
        feed_ids=[feed_id] if feed_id else [],
        folder_ids=[folder_id] if folder_id else [],
        tag_ids=[tag_id] if tag_id else [],
        query=query,
        unread_only=unread,
        starred_only=starred,
        since=since,
        until=until,
        limit=limit,
    )
    entries = repo.select_entries(conn, sel)
    out = []
    for e in entries:
        state = repo.get_state(conn, e.id)
        out.append(
            EntryOut(
                id=e.id,
                feed_id=e.feed_id,
                url=e.url,
                title=e.title,
                author=e.author,
                summary=e.summary,
                published_at=e.published_at,
                read=bool(state and state.read),
                starred=bool(state and state.starred),
                tags=[t.name for t in repo.entry_tags(conn, e.id)],
            )
        )
    return out


@router.get("/{entry_id}")
def get_entry(entry_id: str) -> EntryDetail:
    conn = db()
    entry = repo.get_entry(conn, entry_id, with_body=True)
    if not entry:
        raise HTTPException(404, "Artículo no encontrado")
    state = repo.get_state(conn, entry_id)
    return EntryDetail(
        id=entry.id,
        feed_id=entry.feed_id,
        url=entry.url,
        title=entry.title,
        author=entry.author,
        summary=entry.summary,
        published_at=entry.published_at,
        read=bool(state and state.read),
        starred=bool(state and state.starred),
        tags=[t.name for t in repo.entry_tags(conn, entry_id)],
        body_html=entry.body_html,
        body_text=entry.body_text,
    )


@router.post("/read")
def set_read(req: StateRequest) -> dict:
    with write_tx() as conn:
        n = repo.set_read(conn, req.entry_ids, req.value)
    bus.publish({"type": "state_changed", "entry_ids": req.entry_ids})
    return {"actualizadas": n}


@router.post("/star")
def set_starred(req: StateRequest) -> dict:
    with write_tx() as conn:
        n = repo.set_starred(conn, req.entry_ids, req.value)
    bus.publish({"type": "state_changed", "entry_ids": req.entry_ids})
    return {"actualizadas": n}


class TagRequest(BaseModel):
    entry_id: str
    tag: str
    remove: bool = False


@router.post("/tag")
def tag_entry(req: TagRequest) -> dict:
    with write_tx() as conn:
        tag = repo.get_or_create_tag(conn, req.tag)
        repo.tag_entry(conn, req.entry_id, tag.id, remove=req.remove)
    bus.publish({"type": "state_changed", "entry_ids": [req.entry_id]})
    return {"tag_id": tag.id}


search_router = APIRouter(prefix="/search", tags=["search"], dependencies=[Depends(require_token)])


@search_router.get("")
def search(q: str, limit: int = Query(default=100, le=500)) -> list[EntryOut]:
    """Búsqueda full-text. El índice es FTS5 sin contenido: los fragmentos se
    generan desde el cuerpo comprimido solo para los resultados mostrados."""
    conn = db()
    entries = repo.search(conn, q, limit)
    out = []
    for e in entries:
        state = repo.get_state(conn, e.id)
        out.append(
            EntryOut(
                id=e.id,
                feed_id=e.feed_id,
                url=e.url,
                title=e.title,
                author=e.author,
                summary=e.summary,
                published_at=e.published_at,
                read=bool(state and state.read),
                starred=bool(state and state.starred),
            )
        )
    return out


tags_router = APIRouter(prefix="/tags", tags=["tags"], dependencies=[Depends(require_token)])


@tags_router.get("")
def list_tags() -> list[dict]:
    return [{"id": t.id, "name": t.name, "color": t.color} for t in repo.list_tags(db())]


__all__ = ["router", "search_router", "tags_router"]
