"""Recorrido HTTP real de vista previa, EPUB y correo con SMTP simulado."""

import io
import zipfile

import aiosmtplib
import httpx
import pytest
from rsscore import repo
from rsscore.config import Config, MagazineConfig, SmtpConfig
from rsscore.db import open_db
from rsscore.models import Entry, ExportStatus, Feed
from rsscore.rules.models import Rule
from rsscore.rules.store import save_rule
from rsshub.app import create_app


@pytest.fixture
async def digest_api(tmp_path):
    cfg = Config(
        db_path=tmp_path / "hub.db",
        magazine=MagazineConfig(output_dir=tmp_path, embed_images=False),
        smtp=SmtpConfig(host="smtp.example.test", from_address="reader@example.test",
                        kindle_address="reader@kindle.com"),
    )
    app = create_app(cfg, with_scheduler=False)
    conn = open_db(cfg.db_path)
    feed = repo.add_feed(conn, Feed(url="https://example.test/rss"))
    for name in ("solar", "deportes"):
        repo.insert_entry(conn, Entry(
            id=name, title=name, feed_id=feed.id, guid_hash=name, content_hash=name,
            body_text=f"{name} " + "detalle " * 80, url=f"https://example.test/{name}",
        ))
    rule = Rule.model_validate({
        "name": "Solar",
        "when": {"all": [{"field": "title", "op": "contains", "value": "solar"}]},
        "then": [{"mark_read": True}, {"export": "kindle"}],
    })
    save_rule(conn, rule)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub",
    ) as client:
        yield client, conn, rule
    conn.close()


async def test_preview_no_guarda_ni_ejecuta_acciones(digest_api):
    client, conn, rule = digest_api
    before = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    draft = rule.model_copy(update={"id": "draft", "enabled": False})
    response = await client.post("/rules/preview", json={"rule": draft.model_dump()})
    assert response.status_code == 200, response.text
    assert response.json()["revisadas"] == 2
    assert response.json()["coincidencias"] == 1
    assert response.json()["muestra"][0]["id"] == "solar"
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == before
    assert not repo.get_state(conn, "solar").read
    assert repo.list_exports(conn) == []


async def test_revistas_filtradas_descargables_y_sin_sobrescritura(digest_api):
    client, conn, rule = digest_api
    jobs = []
    for mode in ("full", "excerpt"):
        response = await client.post("/export/magazine", json={
            "selection": {"limit": 1}, "rules": [rule.name],
            "content_mode": mode, "excerpt_words": 30,
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["articulos"] == 1 and not data["enviado_a_kindle"]
        download = await client.get(data["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/epub+zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as book:
            article = book.read("EPUB/art_0001.xhtml").decode()
        assert "solar" in article and "deportes" not in article
        assert article.count("detalle") == (29 if mode == "excerpt" else 80)
        assert repo.get_export(conn, data["job"]).status == ExportStatus.DONE
        jobs.append((data, download.content))
    assert jobs[0][0]["fichero"] != jobs[1][0]["fichero"]
    assert (await client.get(jobs[0][0]["download_url"])).content == jobs[0][1]
    assert not repo.get_state(conn, "solar").read


async def test_envio_adjunta_el_epub_generado(digest_api, monkeypatch):
    client, _, rule = digest_api
    messages = []

    async def smtp_send(message, **kwargs):
        messages.append(message)
        assert kwargs["hostname"] == "smtp.example.test"

    monkeypatch.setattr(aiosmtplib, "send", smtp_send)
    response = await client.post("/export/magazine", json={
        "selection": {}, "rules": [rule.id], "send_to_kindle": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["enviado_a_kindle"]
    assert len(messages) == 1 and messages[0]["To"] == "reader@kindle.com"
    attachment, = messages[0].iter_attachments()
    assert attachment.get_content_type() == "application/epub+zip"
    download = await client.get(response.json()["download_url"])
    assert attachment.get_payload(decode=True) == download.content


async def test_fallo_smtp_conserva_revista_y_registra_error(digest_api, monkeypatch):
    client, conn, _ = digest_api

    async def smtp_send(*args, **kwargs):
        raise aiosmtplib.SMTPException("Servidor temporalmente no disponible")

    monkeypatch.setattr(aiosmtplib, "send", smtp_send)
    response = await client.post("/export/magazine", json={
        "selection": {}, "send_to_kindle": True,
    })
    assert response.status_code == 502, response.text
    job_id = response.json()["detail"]["job"]
    job = repo.get_export(conn, job_id)
    assert job.status == ExportStatus.ERROR and "temporalmente" in job.error
    download = await client.get(f"/export/download/{job_id}")
    assert download.status_code == 200 and zipfile.is_zipfile(io.BytesIO(download.content))


@pytest.mark.parametrize("body", [
    {"selection": {"limit": 0}},
    {"selection": {}, "content_mode": "inventado"},
    {"selection": {}, "excerpt_words": 1},
])
async def test_opciones_invalidas_no_encolan(digest_api, body):
    client, conn, _ = digest_api
    response = await client.post("/export/magazine", json=body)
    assert response.status_code == 422
    assert repo.list_exports(conn) == []


async def test_regla_desconocida_no_genera_revista(digest_api):
    client, conn, _ = digest_api
    response = await client.post("/export/magazine", json={
        "selection": {}, "rules": ["inexistente"],
    })
    assert response.status_code == 400
    job = repo.get_export(conn, response.json()["detail"]["job"])
    assert job.status == ExportStatus.ERROR and "path" not in job.result
