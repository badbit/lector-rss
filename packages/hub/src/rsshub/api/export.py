"""Exportación: Obsidian, Kindle y revistas EPUB.

El móvil no escribe en la bóveda de Obsidian ni tiene credenciales SMTP: encola
un trabajo aquí y lo materializa el hub (Kindle, revista) o el escritorio
(Obsidian, que es quien ve la bóveda local).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from rsscore import repo
from rsscore.models import EntrySelection, ExportJob, ExportKind

from ..deps import bus, config, db, require_token, write_tx

router = APIRouter(prefix="/export", tags=["export"], dependencies=[Depends(require_token)])


class ObsidianRequest(BaseModel):
    entry_ids: list[str]
    target: str = "desktop"  # lo materializa el cliente de escritorio


class KindleRequest(BaseModel):
    entry_ids: list[str] = []
    selection: EntrySelection | None = None
    title: str | None = None


class MagazineRequest(BaseModel):
    selection: EntrySelection
    title: str | None = None
    send_to_kindle: bool = False


@router.post("/obsidian", status_code=202)
def export_obsidian(req: ObsidianRequest) -> dict:
    """Se encola siempre: la bóveda vive en el escritorio, no en el hub."""
    job = ExportJob(
        kind=ExportKind.OBSIDIAN, target=req.target, params={"entry_ids": req.entry_ids}
    )
    with write_tx() as conn:
        repo.enqueue_export(conn, job)
    bus.publish({"type": "export_queued", "job_id": job.id, "target": req.target})
    return {"job_id": job.id, "estado": "encolado", "destino": req.target}


@router.post("/kindle")
async def export_kindle(req: KindleRequest) -> dict:
    from rsscore.export.kindle import send_to_kindle

    cfg = config()
    if not cfg.smtp.host or not cfg.smtp.kindle_address:
        raise HTTPException(400, "Falta configurar SMTP y la dirección @kindle.com")
    conn = db()
    entry_ids = list(req.entry_ids)
    if req.selection:
        entry_ids += [e.id for e in repo.select_entries(conn, req.selection)]
    if not entry_ids:
        raise HTTPException(400, "No hay artículos que enviar")
    try:
        result = await send_to_kindle(conn, entry_ids, cfg.smtp, title=req.title)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo enviar al Kindle: {exc}") from exc
    return {"enviados": len(entry_ids), "resultado": getattr(result, "__dict__", str(result))}


@router.post("/magazine")
async def export_magazine(req: MagazineRequest) -> dict:
    from rsscore.export.magazine import build_magazine

    cfg = config()
    mag_cfg = cfg.magazine.model_copy()
    if req.title:
        mag_cfg.title = req.title
    conn = db()
    result = build_magazine(conn, req.selection, mag_cfg)
    path = getattr(result, "path", result)
    out = {"fichero": str(path), "job": None}

    if req.send_to_kindle:
        from rsscore.export.kindle import send_epub_file

        await send_epub_file(Path(path), cfg.smtp, title=mag_cfg.title)
        out["enviado_a_kindle"] = True
    return out


@router.get("/jobs")
def list_jobs(limit: int = 50) -> list[dict]:
    return [j.model_dump() for j in repo.list_exports(db(), limit)]


@router.get("/jobs/next")
def claim_job(target: str = "desktop") -> dict | None:
    """El worker del escritorio pide trabajo pendiente."""
    with write_tx() as conn:
        job = repo.claim_export(conn, target)
    return job.model_dump() if job else None


class FinishRequest(BaseModel):
    job_id: str
    result: dict = {}
    error: str | None = None


@router.post("/jobs/finish")
def finish_job(req: FinishRequest) -> dict:
    with write_tx() as conn:
        repo.finish_export(conn, req.job_id, result=req.result, error=req.error)
    return {"ok": True}


@router.get("/download/{job_id}")
def download(job_id: str) -> FileResponse:
    jobs = {j.id: j for j in repo.list_exports(db(), 200)}
    job = jobs.get(job_id)
    if not job or not job.result.get("path"):
        raise HTTPException(404, "Ese trabajo no ha producido ningún fichero")
    path = Path(job.result["path"])
    if not path.exists():
        raise HTTPException(410, "El fichero ya no está disponible")
    return FileResponse(path, filename=path.name, media_type="application/epub+zip")
