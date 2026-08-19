"""Aplicación FastAPI del hub.

Deliberadamente sin interfaz web: solo JSON y SSE. Se accede desde el escritorio
y desde Android a través de la VPN de malla; no se expone a Internet.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from rsscore.config import Config

from .api import entries as entries_api
from .api import export as export_api
from .api import feeds as feeds_api
from .api import rules as rules_api
from .api import sync as sync_api
from .deps import configure, db
from .scheduler import build_scheduler

log = logging.getLogger("rsshub")


def create_app(cfg: Config | None = None, *, with_scheduler: bool = True) -> FastAPI:
    cfg = cfg or Config.load()
    configure(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        if with_scheduler:
            scheduler = build_scheduler(cfg)
            scheduler.start()
            log.info("Planificador arrancado")
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="rsshub",
        version="0.1.0",
        summary="Hub de sincronización del lector RSS (sin interfaz web)",
        lifespan=lifespan,
    )

    app.include_router(feeds_api.router)
    app.include_router(feeds_api.folders_router)
    app.include_router(feeds_api.opml_router)
    app.include_router(entries_api.router)
    app.include_router(entries_api.search_router)
    app.include_router(entries_api.tags_router)
    app.include_router(sync_api.router)
    app.include_router(rules_api.router)
    app.include_router(rules_api.smart_router)
    app.include_router(export_api.router)

    @app.get("/health", tags=["meta"])
    def health() -> JSONResponse:
        conn = db()
        return JSONResponse(
            {
                "ok": True,
                "version": "0.1.0",
                "feeds": conn.execute(
                    "SELECT COUNT(*) AS n FROM feeds WHERE deleted = 0"
                ).fetchone()["n"],
                "entradas": conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"],
                "sin_leer": conn.execute(
                    "SELECT COUNT(*) AS n FROM entry_state WHERE read = 0"
                ).fetchone()["n"],
            }
        )

    return app
