from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from rsscore import repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.ids import hash_content, hash_guid
from rsscore.models import Entry, ExportJob, ExportKind, ExportStatus, Feed, SyncScope
from rsscore.sync import build_snapshot
from rssdesk.tasks import Backend


def _inline_to_thread(monkeypatch):
    async def inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    # El sandbox de pruebas no ejecuta los hilos del executor. Aquí interesa el
    # protocolo del worker; el exportador tiene su propia batería.
    monkeypatch.setattr(asyncio, "to_thread", inline)


@pytest.mark.asyncio
async def test_worker_recoge_del_hub_y_exporta_el_cuerpo_completo(tmp_path, monkeypatch):
    _inline_to_thread(monkeypatch)
    hub_db = open_db(tmp_path / "hub.db", device_name="hub")
    feed = repo.add_feed(
        hub_db,
        Feed(id="feed-prueba", url="https://example.test/feed", title="Feed de prueba"),
    )
    entry = Entry(
        id="entrada-remota",
        feed_id=feed.id,
        guid_hash=hash_guid(feed.id, "entrada-remota"),
        content_hash=hash_content("Título remoto", "Texto completo remoto"),
        url="https://example.test/articulo",
        title="Título remoto",
        summary="Resumen corto",
        body_html="<p>Texto <strong>completo remoto</strong>.</p>",
        body_text="Texto completo remoto.",
    )
    repo.insert_entry(hub_db, entry, track=True)
    snapshot = build_snapshot(hub_db, SyncScope(days=None))
    hub_db.close()

    job = ExportJob(
        id="trabajo-remoto",
        kind=ExportKind.OBSIDIAN,
        target="desktop",
        params={"entry_ids": [entry.id]},
    )
    finished = {}

    def hub(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sync/register":
            return httpx.Response(200, json={})
        if path == "/sync/snapshot":
            return httpx.Response(200, json=snapshot)
        if path == "/sync/pull":
            return httpx.Response(
                200,
                json={"ops": [], "cursor": snapshot["cursor"], "has_more": False},
            )
        if path == "/export/jobs/next":
            return httpx.Response(200, json=job.model_dump(mode="json"))
        if path == f"/entries/{entry.id}":
            return httpx.Response(
                200,
                json={
                    "id": entry.id,
                    "feed_id": entry.feed_id,
                    "url": entry.url,
                    "title": entry.title,
                    "author": entry.author,
                    "summary": entry.summary,
                    "published_at": entry.published_at,
                    "read": False,
                    "starred": False,
                    "tags": [],
                    "body_html": entry.body_html,
                    "body_text": entry.body_text,
                },
            )
        if path == "/export/jobs/finish":
            finished.update(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"Petición inesperada: {request.method} {request.url}")

    desktop_cfg = Config(
        db_path=tmp_path / "desktop.db",
        device_name="escritorio",
        hub_url="http://hub",
        obsidian={"vault_path": tmp_path / "vault"},
    )
    desktop_db = open_db(desktop_cfg.db_path, device_name="escritorio")
    transport = httpx.MockTransport(hub)
    async with httpx.AsyncClient(transport=transport, base_url="http://hub") as client:
        backend = Backend(desktop_db, desktop_cfg, http_client=client)
        assert await backend.procesar_exportacion_pendiente()

    desktop_db.close()
    assert finished["job_id"] == job.id
    assert finished["error"] is None
    assert finished["result"]["count"] == 1
    note = next((tmp_path / "vault" / "Clippings").glob("*.md"))
    text = note.read_text(encoding="utf-8")
    assert "Texto **completo remoto**." in text
    assert "Resumen corto" not in text


@pytest.mark.asyncio
async def test_worker_conserva_la_cola_local_en_modo_autonomo(tmp_path, monkeypatch):
    _inline_to_thread(monkeypatch)
    cfg = Config(
        db_path=tmp_path / "local.db",
        obsidian={"vault_path": tmp_path / "vault"},
    )
    db = open_db(cfg.db_path, device_name="autonomo")
    feed = repo.add_feed(db, Feed(url="https://example.test/feed", title="Feed local"))
    entry = Entry(
        feed_id=feed.id,
        guid_hash=hash_guid(feed.id, "local"),
        content_hash=hash_content("Local", "Cuerpo local"),
        title="Artículo local",
        body_html="<p>Cuerpo local</p>",
        body_text="Cuerpo local",
    )
    repo.insert_entry(db, entry)
    job = ExportJob(
        kind=ExportKind.OBSIDIAN,
        target="desktop",
        params={"entry_ids": [entry.id]},
    )
    repo.enqueue_export(db, job)

    assert await Backend(db, cfg).procesar_exportacion_pendiente()

    saved = repo.list_exports(db)[0]
    db.close()
    assert saved.status == ExportStatus.DONE
    assert saved.result["count"] == 1
