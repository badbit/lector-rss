"""Suscripciones: feeds, carpetas y OPML."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from rsscore import repo
from rsscore.models import Feed, Folder

from ..deps import bus, config, db, require_token, write_tx

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(require_token)])
folders_router = APIRouter(
    prefix="/folders", tags=["folders"], dependencies=[Depends(require_token)]
)
opml_router = APIRouter(prefix="/opml", tags=["opml"], dependencies=[Depends(require_token)])


class AddFeedRequest(BaseModel):
    url: str
    folder_id: str | None = None
    title: str | None = None
    fetch_full_text: bool = False
    # Para webs sin feed: 'scrape' (listado de artículos) o 'watch' (cambios).
    source_kind: str = "feed"
    source_config: dict = {}


class PreviewRequest(BaseModel):
    url: str


class PreviewCandidate(BaseModel):
    item_selector: str
    title_selector: str = ""
    date_selector: str = ""
    count: int
    score: float
    sample: list[str] = []


class PreviewResponse(BaseModel):
    url: str
    has_feed: bool
    feed_url: str | None = None
    javascript_rendered: bool = False
    candidates: list[PreviewCandidate] = []


class FeedOut(BaseModel):
    id: str
    url: str
    site_url: str | None
    title: str
    folder_id: str | None
    unread: int = 0
    error: str | None = None
    disabled: bool = False


@router.get("")
def list_feeds() -> list[FeedOut]:
    conn = db()
    counts = repo.unread_counts(conn)
    return [
        FeedOut(
            id=f.id,
            url=f.url,
            site_url=f.site_url,
            title=f.display_title,
            folder_id=f.folder_id,
            unread=counts.get(f.id, 0),
            error=f.last_error,
            disabled=f.disabled,
        )
        for f in repo.list_feeds(conn)
    ]


@router.post("", status_code=201)
async def add_feed(req: AddFeedRequest) -> FeedOut:
    """Da de alta un feed. Si la URL es una página HTML, descubre su feed real."""
    from rsscore.ingest import (
        Ingestor,  # import diferido: arranque más rápido
        NoFeedFound,
    )

    cfg = config()
    conn = db()
    ingestor = Ingestor(conn, cfg)
    try:
        if req.source_kind == "feed":
            feed = await ingestor.add_by_url(req.url, folder_id=req.folder_id)
        else:
            feed = await ingestor.add_source(
                req.url, req.source_kind, req.source_config,
                folder_id=req.folder_id, title=req.title or "",
            )
    except NoFeedFound as exc:
        # 404 con las propuestas dentro: el cliente puede ofrecer raspar.
        raise HTTPException(
            404,
            {
                "mensaje": str(exc),
                "candidatos": [
                    {"item_selector": c.config.item_selector, "count": c.count,
                     "sample": c.sample}
                    for c in exc.candidates
                ],
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(400, f"No se pudo dar de alta el feed: {exc}") from exc
    if req.title:
        with write_tx() as c:
            repo.update_feed_meta(c, feed.id, custom_title=req.title)
    if req.fetch_full_text:
        with write_tx() as c:
            repo.update_feed_meta(c, feed.id, fetch_full_text=1)
    bus.publish({"type": "feeds_changed"})
    counts = repo.unread_counts(conn)
    return FeedOut(
        id=feed.id,
        url=feed.url,
        site_url=feed.site_url,
        title=feed.display_title,
        folder_id=feed.folder_id,
        unread=counts.get(feed.id, 0),
    )


@router.post("/preview")
async def preview(req: PreviewRequest) -> PreviewResponse:
    """Mira qué se podría extraer de una URL, sin escribir nada.

    Es lo que permite a los clientes enseñar el resultado antes de dar de alta,
    en vez de crear a ciegas una suscripción que quizá no funcione.
    """
    from rsscore.ingest import Ingestor
    from rsscore.parse import parse_feed
    from rsscore.scrape import guess_selectors, looks_javascript_rendered

    ingestor = Ingestor(db(), config())
    try:
        respuesta = await ingestor.fetcher.get(req.url)
        if not respuesta.ok or not respuesta.content:
            raise HTTPException(502, f"No se pudo descargar: {respuesta.error}")
        base = respuesta.final_url or req.url

        parsed = parse_feed(respuesta.content, "preview", base_url=base)
        if parsed.is_feed:
            return PreviewResponse(url=base, has_feed=True, feed_url=base)

        html = respuesta.text()
        return PreviewResponse(
            url=base,
            has_feed=False,
            javascript_rendered=looks_javascript_rendered(html),
            candidates=[
                PreviewCandidate(
                    item_selector=c.config.item_selector,
                    title_selector=c.config.title_selector,
                    date_selector=c.config.date_selector,
                    count=c.count, score=round(c.score, 2), sample=c.sample,
                )
                for c in guess_selectors(html, base)
            ],
        )
    finally:
        await ingestor.aclose()


@router.delete("/{feed_id}", status_code=204)
def delete_feed(feed_id: str) -> Response:
    with write_tx() as conn:
        if not repo.get_feed(conn, feed_id):
            raise HTTPException(404, "Feed no encontrado")
        repo.delete_feed(conn, feed_id)
    bus.publish({"type": "feeds_changed"})
    return Response(status_code=204)


@router.patch("/{feed_id}")
def update_feed(
    feed_id: str,
    folder_id: str | None = None,
    title: str | None = None,
    interval_seconds: int | None = None,
    fetch_full_text: bool | None = None,
) -> dict:
    with write_tx() as conn:
        if not repo.get_feed(conn, feed_id):
            raise HTTPException(404, "Feed no encontrado")
        if folder_id is not None:
            repo.set_feed_folder(conn, feed_id, folder_id or None)
        meta: dict = {}
        if title is not None:
            meta["custom_title"] = title
        if interval_seconds is not None:
            meta["interval_seconds"] = interval_seconds
        if fetch_full_text is not None:
            meta["fetch_full_text"] = int(fetch_full_text)
        if meta:
            repo.update_feed_meta(conn, feed_id, **meta)
    bus.publish({"type": "feeds_changed"})
    return {"ok": True}


@router.post("/{feed_id}/refresh")
async def refresh_feed(feed_id: str) -> dict:
    from rsscore.ingest import Ingestor

    conn = db()
    feed = repo.get_feed(conn, feed_id)
    if not feed:
        raise HTTPException(404, "Feed no encontrado")
    result = await Ingestor(conn, config()).refresh_feed(feed)
    bus.publish({"type": "entries_changed", "feed_id": feed_id})
    return {
        "feed_id": feed_id,
        "nuevas": len(result.new_entries),
        "duplicadas_eliminadas": result.duplicates_removed,
        "estado": result.status,
    }


@router.post("/refresh")
async def refresh_all(force: bool = False) -> dict:
    from rsscore.ingest import Ingestor

    ingestor = Ingestor(db(), config())
    results = await (ingestor.refresh_all() if force else ingestor.refresh_due())
    total = sum(len(r.new_entries) for r in results)
    duplicadas = sum(r.duplicates_removed for r in results)
    bus.publish({"type": "entries_changed"})
    return {"feeds": len(results), "nuevas": total, "duplicadas_eliminadas": duplicadas}


@router.post("/{feed_id}/read")
def mark_feed_read(feed_id: str) -> dict:
    with write_tx() as conn:
        n = repo.mark_feed_read(conn, feed_id)
    bus.publish({"type": "state_changed"})
    return {"marcadas": n}


# ------------------------------------------------------------------ carpetas
@folders_router.get("")
def list_folders() -> list[Folder]:
    return repo.list_folders(db())


@folders_router.post("", status_code=201)
def create_folder(folder: Folder) -> Folder:
    with write_tx() as conn:
        repo.upsert_folder(conn, folder)
    bus.publish({"type": "feeds_changed"})
    return folder


# ---------------------------------------------------------------------- OPML
@opml_router.post("/import")
async def import_opml(request: Request) -> dict:
    """Recibe el fichero OPML como cuerpo crudo, no como formulario."""
    from rsscore.opml import import_opml as do_import

    cuerpo = await request.body()
    with write_tx() as conn:
        result = do_import(conn, cuerpo)
    bus.publish({"type": "feeds_changed"})
    return result.as_dict() if hasattr(result, "as_dict") else {"importados": result}


@opml_router.get("/export")
def export_opml() -> Response:
    from rsscore.opml import export_opml as do_export

    xml = do_export(db())
    return Response(
        content=xml,
        media_type="text/x-opml",
        headers={"Content-Disposition": 'attachment; filename="suscripciones.opml"'},
    )


__all__ = ["Feed", "folders_router", "opml_router", "router"]
