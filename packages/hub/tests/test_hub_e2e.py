"""Prueba de extremo a extremo: hub + dos dispositivos sincronizando por HTTP.

No se simula la capa de red del cliente: `SyncClient` habla con la aplicación
FastAPI real a través del transporte ASGI, así que se ejercitan los mismos
endpoints, la misma serialización y la misma autenticación que en producción.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from rsscore import repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.models import EntrySelection, SyncScope
from rsscore.sync import SyncClient
from rsshub.app import create_app

FEED_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Diario Hub</title><link>https://hub.example</link>
  <item><title>Noticia uno</title><link>https://hub.example/1</link><guid>h-1</guid>
        <pubDate>Mon, 17 Aug 2026 10:00:00 +0000</pubDate>
        <description>Cuerpo de la primera noticia</description></item>
  <item><title>Noticia dos</title><link>https://hub.example/2</link><guid>h-2</guid>
        <pubDate>Mon, 17 Aug 2026 11:00:00 +0000</pubDate>
        <description>Cuerpo de la segunda noticia</description></item>
</channel></rss>"""


@pytest.fixture
def hub(tmp_path):
    cfg = Config.load()
    cfg.db_path = tmp_path / "hub.db"
    cfg.hub.tokens = []            # sin tokens: acceso local sin autenticación
    app = create_app(cfg, with_scheduler=False)
    return app, cfg


@pytest.fixture
def http(hub):
    app, _ = hub
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")


async def test_health(http):
    r = await http.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_health_ofrece_las_fuentes(http):
    """El artículo 13 de la AGPL obliga a ofrecer el código a quien use el hub
    por red. Sin interfaz web, /health es el único sitio donde todo cliente
    mira, así que el aviso vive ahí y esta prueba impide que se borre sin más."""
    cuerpo = (await http.get("/health")).json()
    assert cuerpo["license"] == "AGPL-3.0-or-later"
    assert cuerpo["source"].startswith("http")


@respx.mock
async def test_alta_de_feed_y_lectura(http, hub):
    _, cfg = hub
    respx.get("https://hub.example/feed").mock(
        return_value=httpx.Response(200, content=FEED_XML,
                                    headers={"Content-Type": "application/rss+xml"})
    )
    r = await http.post("/feeds", json={"url": "https://hub.example/feed"})
    assert r.status_code == 201, r.text
    feed_id = r.json()["id"]

    entradas = (await http.get("/entries")).json()
    assert len(entradas) == 2
    assert (await http.get("/feeds")).json()[0]["unread"] == 2

    detalle = (await http.get(f"/entries/{entradas[0]['id']}")).json()
    assert "Cuerpo de la" in (detalle["body_text"] or detalle["body_html"] or "")

    r = await http.post("/entries/read", json={"entry_ids": [entradas[0]["id"]], "value": True})
    assert r.json()["actualizadas"] == 1
    assert (await http.get("/feeds")).json()[0]["unread"] == 1

    # La búsqueda full-text funciona sobre lo recién ingerido.
    assert (await http.get("/search", params={"q": "segunda"})).json()


@respx.mock
async def test_dos_dispositivos_convergen_a_traves_del_hub(http, hub, tmp_path):
    _, cfg = hub
    respx.get("https://hub.example/feed").mock(
        return_value=httpx.Response(200, content=FEED_XML,
                                    headers={"Content-Type": "application/rss+xml"})
    )
    await http.post("/feeds", json={"url": "https://hub.example/feed"})

    portatil = open_db(tmp_path / "portatil.db", device_name="portatil")
    movil = open_db(tmp_path / "movil.db", device_name="movil")

    cliente_a = SyncClient(portatil, "http://hub", scope=SyncScope(days=30), client=http)
    cliente_b = SyncClient(movil, "http://hub", scope=SyncScope(days=30), client=http)

    # Arranque: los dos se traen la foto del hub.
    assert (await cliente_a.sync_once(name="portatil")).bootstrap
    assert (await cliente_b.sync_once(name="movil")).bootstrap
    assert len(repo.list_feeds(portatil)) == 1
    entradas = repo.select_entries(portatil, EntrySelection(limit=10))
    assert len(entradas) == 2

    # El portátil lee un artículo estando desconectado.
    objetivo = entradas[0].id
    repo.set_read(portatil, [objetivo], True)
    repo.set_starred(portatil, [objetivo], True)
    await cliente_a.sync_once()

    # El móvil sincroniza y ve exactamente lo mismo.
    await cliente_b.sync_once()
    estado_movil = repo.get_state(movil, objetivo)
    assert estado_movil.read is True
    assert estado_movil.starred is True

    # Y el hub también.
    detalle = (await http.get(f"/entries/{objetivo}")).json()
    assert detalle["read"] is True and detalle["starred"] is True


@respx.mock
async def test_conflicto_entre_dispositivos_se_resuelve_igual_en_todos(http, hub, tmp_path):
    respx.get("https://hub.example/feed").mock(
        return_value=httpx.Response(200, content=FEED_XML,
                                    headers={"Content-Type": "application/rss+xml"})
    )
    await http.post("/feeds", json={"url": "https://hub.example/feed"})

    a = open_db(tmp_path / "a.db", device_name="a")
    b = open_db(tmp_path / "b.db", device_name="b")
    ca = SyncClient(a, "http://hub", client=http)
    cb = SyncClient(b, "http://hub", client=http)
    await ca.sync_once(name="a")
    await cb.sync_once(name="b")

    from rsscore.models import EntrySelection

    objetivo = repo.select_entries(a, EntrySelection(limit=1))[0].id

    # Los dos, sin conexión, hacen cosas opuestas sobre el mismo artículo.
    repo.set_read(a, [objetivo], True)
    b.execute("UPDATE node SET lamport = 500 WHERE id = 1")   # el móvil escribe después
    repo.set_read(b, [objetivo], False)

    for _ in range(2):        # dos vueltas: subir y bajar en ambos sentidos
        await ca.sync_once()
        await cb.sync_once()

    estados = {
        "a": repo.get_state(a, objetivo).read,
        "b": repo.get_state(b, objetivo).read,
        "hub": (await http.get(f"/entries/{objetivo}")).json()["read"],
    }
    assert len(set(estados.values())) == 1, f"no convergieron: {estados}"
    assert estados["a"] is False, "gana la escritura con reloj mayor"


async def test_opml_ida_y_vuelta(http):
    opml = b"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline text="Tec"><outline type="rss" text="X" xmlUrl="https://x.example/f"/></outline>
    </body></opml>"""
    r = await http.post("/opml/import", content=opml)
    assert r.status_code == 200, r.text
    assert (await http.get("/feeds")).json()

    exportado = await http.get("/opml/export")
    assert b"x.example" in exportado.content


async def test_token_obligatorio_cuando_esta_configurado(tmp_path):
    from pydantic import SecretStr

    cfg = Config.load()
    cfg.db_path = tmp_path / "seguro.db"
    cfg.hub.tokens = [SecretStr("clave-secreta")]
    app = create_app(cfg, with_scheduler=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub"
    ) as c:
        assert (await c.get("/feeds")).status_code == 401
        assert (await c.get("/feeds", headers={"Authorization": "Bearer mal"})).status_code == 403
        ok = await c.get("/feeds", headers={"Authorization": "Bearer clave-secreta"})
        assert ok.status_code == 200
