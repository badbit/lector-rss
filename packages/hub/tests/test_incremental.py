"""Sincronización real por HTTP después de la copia inicial."""

from __future__ import annotations

import httpx
import pytest
from rsscore import repo
from rsscore.config import Config
from rsscore.db import get_setting, open_db
from rsscore.ids import now_ms
from rsscore.models import Entry, Feed, Folder, SyncScope
from rsscore.sync import SyncClient
from rsshub.app import create_app


@pytest.fixture
async def pair(tmp_path):
    cfg = Config(db_path=tmp_path / "hub.db")
    app = create_app(cfg, with_scheduler=False)
    hub = open_db(cfg.db_path)
    local = open_db(tmp_path / "client.db")
    feed = repo.add_feed(hub, Feed(url="https://example.test/rss", title="Noticias"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub",
    ) as http:
        sync = SyncClient(local, "http://hub", client=http)
        yield hub, local, feed, http, sync
    hub.close()
    local.close()


def add(conn, feed, ident, *, old=False):
    repo.insert_entry(conn, Entry(
        id=ident, feed_id=feed.id, guid_hash=ident, content_hash=ident,
        title=f"Noticia {ident}", summary="Contenido buscable",
        published_at=now_ms() - (400 * 86_400_000 if old else 0),
    ))


async def test_llegan_articulos_despues_del_arranque_y_son_buscables(pair):
    hub, local, feed, _, sync = pair
    add(hub, feed, "primero")
    await sync.sync_once()
    add(hub, feed, "segundo")
    stats = await sync.sync_once()
    assert stats.articulos == 1
    assert repo.get_entry(local, "segundo").title == "Noticia segundo"
    assert repo.search(local, "buscable")
    assert (await sync.sync_once()).articulos == 0


async def test_las_paginas_filtradas_avanzan_hasta_las_altas_visibles(pair, monkeypatch):
    monkeypatch.setattr("rsscore.sync.client.LOTE_ENTRADAS", 2)
    hub, local, feed, http, sync = pair
    sync.scope = SyncScope(days=7, include_unread=False, include_starred=False)
    await sync.sync_once()
    for i in range(4):
        add(hub, feed, f"viejo{i}", old=True)
    add(hub, feed, "nuevo")
    first = (await http.get("/sync/pull", params={
        "device_id": sync.device_id, "entries_since": 0, "entries_limit": 2,
    })).json()
    assert first["entries"] == []
    assert first["entries_cursor"] == 2 and first["entries_has_more"]
    await sync.sync_once()
    assert repo.get_entry(local, "nuevo")
    assert repo.get_entry(local, "viejo0") is None
    assert int(get_setting(local, "sync_entries_cursor")) == 5


async def test_un_articulo_antiguo_entra_al_guardarlo_con_sus_etiquetas(pair):
    hub, local, feed, _, sync = pair
    add(hub, feed, "antiguo", old=True)
    repo.set_read(hub, ["antiguo"], True)
    await sync.sync_once()
    assert repo.get_entry(local, "antiguo") is None
    tag = repo.get_or_create_tag(hub, "investigación")
    repo.tag_entry(hub, "antiguo", tag.id)
    repo.set_starred(hub, ["antiguo"], True)
    await sync.sync_once()
    assert repo.get_state(local, "antiguo").read
    assert repo.get_state(local, "antiguo").starred
    assert repo.entry_tags(local, "antiguo")[0].name == "investigación"


async def test_hidratar_no_sobrescribe_cambios_locales_ni_cuerpos(pair):
    hub, local, feed, _, sync = pair
    add(hub, feed, "uno")
    await sync.sync_once()
    repo.update_entry_body(local, "uno", html="<p>local</p>", text="local")
    repo.set_starred(hub, ["uno"], True)
    local.execute("UPDATE node SET lamport = 9999")
    repo.set_read(local, ["uno"], True)
    queued = repo.outbox_batch(local)
    await sync.pull()  # Descargar mientras todavía hay cambios sin subir.
    assert repo.get_state(local, "uno").read
    assert repo.get_state(local, "uno").starred
    assert repo.get_entry(local, "uno").body_text == "local"
    assert repo.outbox_batch(local) == queued


async def test_las_altas_respetan_el_filtro_de_suscripciones(pair):
    hub, local, feed, _, sync = pair
    sync.scope = SyncScope(feed_ids=[feed.id])
    await sync.sync_once()
    other = repo.add_feed(hub, Feed(url="https://example.test/fuera"))
    add(hub, feed, "dentro")
    add(hub, other, "fuera")
    repo.set_starred(hub, ["fuera"], True)
    await sync.sync_once()
    assert repo.get_entry(local, "dentro") is not None
    assert repo.get_entry(local, "fuera") is None


async def test_dependencias_completas_aunque_el_diario_este_paginado(pair, monkeypatch):
    monkeypatch.setattr("rsscore.sync.client.LOTE_BAJADA", 1)
    hub, local, feed, http, sync = pair
    await sync.sync_once()
    parent = repo.upsert_folder(hub, Folder(name="Padre"))
    child = repo.upsert_folder(hub, Folder(name="Hija", parent_id=parent.id))
    new_feed = repo.add_feed(hub, Feed(
        url="https://example.test/otro", title="Otro", folder_id=child.id,
    ))
    add(hub, new_feed, "nuevo")
    await sync.sync_once()
    assert repo.get_entry(local, "nuevo").feed_id == new_feed.id
    assert repo.get_feed(local, new_feed.id).folder_id == child.id
    assert repo.folder_by_name(local, "Hija").parent_id == parent.id


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"since": -1}, {"entries_since": -1}, {"entries_limit": 0},
])
async def test_rechaza_cursores_y_tamanos_invalidos(pair, params):
    _, _, _, http, _ = pair
    assert (await http.get("/sync/pull", params=params)).status_code == 422


async def test_el_hub_solo_confirma_el_cursor_que_el_cliente_ya_recibio(pair):
    hub, _, _, http, sync = pair
    await sync.register()
    response = await http.get("/sync/pull", params={"device_id": sync.device_id})
    assert response.json()["cursor"] > 0
    row = hub.execute(
        "SELECT last_seq FROM sync_clients WHERE device_id = ?", (sync.device_id,),
    ).fetchone()
    assert row[0] == 0


async def test_una_pagina_invalida_revierte_articulos_y_cursores(pair):
    _, local, feed, _, sync = pair
    await sync.sync_once()
    before = sync._cursor()
    arrivals_before = get_setting(local, "sync_entries_cursor", "0")
    entry = Entry(id="fallida", feed_id=feed.id, guid_hash="fallida", content_hash="fallida")
    response = {
        "entries": [entry.model_dump()], "cursor": 999, "entries_cursor": 999,
        "ops": [{
            "entity": "feed", "entity_id": feed.id, "field": "title", "value": None,
            "lamport": 9999, "device_id": "hub", "ts": 0,
        }],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        base_url="http://hub",
    ) as http:
        client = SyncClient(local, "http://hub", client=http)
        with pytest.raises(ValueError, match="Cambios inválidos"):
            await client.pull()
    assert repo.get_entry(local, "fallida") is None
    assert sync._cursor() == before
    assert get_setting(local, "sync_entries_cursor", "0") == arrivals_before


def test_migracion_incluye_articulos_existentes_y_registra_altas(tmp_path):
    path = tmp_path / "previous.db"
    conn = open_db(path)
    conn.execute("DROP TRIGGER entries_arrival")
    conn.execute("DROP TABLE entry_arrivals")
    conn.execute("PRAGMA user_version = 4")
    feed = repo.add_feed(conn, Feed(url="https://example.test/rss"))
    add(conn, feed, "existente")
    conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("SELECT entry_id FROM entry_arrivals").fetchone()[0] == "existente"
        add(conn, feed, "nuevo")
        assert [r[0] for r in conn.execute(
            "SELECT entry_id FROM entry_arrivals ORDER BY seq",
        )] == ["existente", "nuevo"]
    finally:
        conn.close()
