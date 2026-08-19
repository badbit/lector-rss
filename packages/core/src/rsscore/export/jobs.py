"""Cola de exportaciones: ejecuta los trabajos que encolan el móvil y el hub.

El reparto es el que impone la realidad de cada máquina: la bóveda de Obsidian
solo la ve el escritorio y las credenciales SMTP solo están en el hub, así que
quien encola no es quien ejecuta. Un trabajo se toma con `repo.claim_export` y
se cierra **siempre** con `repo.finish_export`, con resultado o con error: un
trabajo que se queda en «running» para siempre es peor que uno fallido, porque
nadie lo reintenta ni aparece como problema en la interfaz.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .. import repo
from ..models import EntrySelection, ExportJob, ExportKind

log = logging.getLogger(__name__)


async def run_export_job(conn: sqlite3.Connection, job: ExportJob, cfg: Any) -> None:
    """Ejecuta un trabajo y lo cierra. No propaga la excepción: la guarda."""
    try:
        resultado = await _dispatch(conn, job, cfg)
    # Se captura todo a propósito: aquí el error no es una excepción que
    # propagar, es el resultado del trabajo, y tiene que quedar guardado.
    except Exception as exc:
        log.warning("Exportación %s (%s) fallida: %s", job.id, job.kind, exc)
        repo.finish_export(conn, job.id, error=describe_error(exc))
    else:
        repo.finish_export(conn, job.id, result=resultado)


async def _dispatch(conn: sqlite3.Connection, job: ExportJob, cfg: Any) -> dict[str, Any]:
    match job.kind:
        case ExportKind.OBSIDIAN:
            return await _run_obsidian(conn, job, cfg)
        case ExportKind.KINDLE:
            return await _run_kindle(conn, job, cfg)
        case ExportKind.MAGAZINE:
            return await _run_magazine(conn, job, cfg)
        case _:
            raise ValueError(f"Tipo de exportación desconocido: {job.kind}")


# ------------------------------------------------------------------ ejecutores
async def _run_obsidian(
    conn: sqlite3.Connection, job: ExportJob, cfg: Any
) -> dict[str, Any]:
    from .obsidian import export_to_obsidian

    ids = _entry_ids(conn, job)
    if not ids:
        raise ValueError("El trabajo no trae ningún artículo que exportar")
    # Escribir en la bóveda es E/S de disco pura y bloqueante: fuera del bucle.
    rutas = await asyncio.to_thread(export_to_obsidian, conn, ids, cfg.obsidian)
    return {"paths": [str(p) for p in rutas], "count": len(rutas)}


async def _run_kindle(conn: sqlite3.Connection, job: ExportJob, cfg: Any) -> dict[str, Any]:
    from .kindle import send_to_kindle

    ids = _entry_ids(conn, job)
    if not ids:
        raise ValueError("El trabajo no trae ningún artículo que enviar")
    resultado = await send_to_kindle(
        conn, ids, cfg.smtp, title=job.params.get("title"), magazine_cfg=cfg.magazine
    )
    return {
        "messages": resultado.messages,
        "articles": resultado.articles,
        "filenames": resultado.filenames,
        "bytes": resultado.bytes_sent,
    }


async def _run_magazine(
    conn: sqlite3.Connection, job: ExportJob, cfg: Any
) -> dict[str, Any]:
    from .magazine import build_magazine

    seleccion = _selection(job) or EntrySelection(limit=cfg.magazine.max_articles)
    mag = cfg.magazine.model_copy()
    if titulo := job.params.get("title"):
        mag.title = titulo
    salida = job.params.get("out_path")

    resultado = await asyncio.to_thread(
        build_magazine, conn, seleccion, mag, out_path=Path(salida) if salida else None
    )
    respuesta = {
        "path": str(resultado.path),
        "articles": resultado.articles,
        "sections": [list(s) for s in resultado.sections],
        "words": resultado.stats.words,
        "minutes": resultado.stats.minutes,
        "bytes": resultado.size_bytes,
    }

    if job.params.get("send_to_kindle"):
        from .kindle import send_epub_file

        envio = await send_epub_file(resultado.path, cfg.smtp, title=mag.title)
        respuesta["kindle_messages"] = envio.messages
    return respuesta


def _entry_ids(conn: sqlite3.Connection, job: ExportJob) -> list[str]:
    """Identificadores del trabajo, ya vengan sueltos o dentro de una selección."""
    ids = [str(i) for i in job.params.get("entry_ids") or []]
    if seleccion := _selection(job):
        ids += [e.id for e in repo.select_entries(conn, seleccion) if e.id not in ids]
    return ids


def _selection(job: ExportJob) -> EntrySelection | None:
    datos = job.params.get("selection")
    if not datos:
        return None
    return EntrySelection.model_validate(datos)


def describe_error(exc: BaseException) -> str:
    """Texto legible para guardar en el trabajo y enseñar en la interfaz."""
    mensaje = str(exc).strip()
    if not mensaje:
        return type(exc).__name__
    # Los errores propios (KindleError, ValueError con mensaje nuestro) ya vienen
    # redactados para el usuario; el resto se etiqueta con su tipo para poder
    # rastrearlos en los registros.
    if type(exc).__name__ in {"KindleError", "ValueError"}:
        return mensaje
    return f"{type(exc).__name__}: {mensaje}"


# --------------------------------------------------------------------- worker
async def worker_loop(
    conn: sqlite3.Connection,
    cfg: Any,
    target: str,
    *,
    poll_seconds: float = 5,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Consume trabajos para este destino hasta que se pida parar.

    Sondea en vez de escuchar notificaciones porque el escritorio puede estar
    apagado cuando el móvil encola: al arrancar recoge lo pendiente sin que
    nadie tenga que reenviar nada. Devuelve cuántos trabajos ha procesado.
    """
    parar = stop_event or asyncio.Event()
    hechos = 0
    while not parar.is_set():
        try:
            job = repo.claim_export(conn, target)
        except sqlite3.Error as exc:
            log.warning("No se pudo tomar trabajo de exportación: %s", exc)
            job = None

        if job is not None:
            await run_export_job(conn, job, cfg)
            hechos += 1
            continue   # puede haber más en cola: no se espera

        # Espera interrumpible: si llega la orden de parar no hay que aguantar
        # el intervalo entero.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(parar.wait(), timeout=poll_seconds)
    return hechos
