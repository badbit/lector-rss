"""Regresiones del arranque por snapshot y su selección parcial del archivo."""

from __future__ import annotations

import sqlite3

import pytest
from rsscore import repo
from rsscore.db import open_db
from rsscore.ids import hash_content, hash_guid, now_ms
from rsscore.models import ChangeOp, Entity, Entry, Feed, SyncScope
from rsscore.sync import apply_ops, apply_snapshot, build_snapshot
from rsscore.sync.scope import entries_in_scope, is_entry_in_scope
from rsscore.sync.snapshot import iter_snapshot_chunks


@pytest.fixture
def nodes(tmp_path):
    hub = open_db(tmp_path / "hub.db")
    client = open_db(tmp_path / "client.db")
    feed = repo.add_feed(hub, Feed(url="https://example.org/feed", title="Noticias"))
    for i in range(5):
        repo.insert_entry(hub, Entry(
            id=f"E{i}", feed_id=feed.id, guid_hash=hash_guid(feed.id, str(i)),
            content_hash=hash_content(str(i)), title=f"Noticia {i}",
            published_at=now_ms() - 400 * 86_400_000,
        ))
        repo.set_read(hub, [f"E{i}"], True)
    yield hub, client, feed
    hub.close()
    client.close()


@pytest.mark.parametrize("unread,starred", [(True, True), (False, True), (True, False)])
def test_sin_limite_temporal_incluye_tambien_lo_leido(nodes, unread, starred):
    hub, _, _ = nodes
    scope = SyncScope(days=None, include_unread=unread, include_starred=starred)
    assert is_entry_in_scope(hub, "E0", scope)
    assert len(entries_in_scope(hub, scope)) == 5


def test_snapshot_conserva_relojes_por_campo_y_rechaza_cambios_antiguos(nodes):
    hub, client, feed = nodes
    repo.set_starred(hub, ["E0"], True)
    read_clock = repo.field_clock(hub, Entity.ENTRY_STATE, "E0", "read")
    star_clock = repo.field_clock(hub, Entity.ENTRY_STATE, "E0", "starred")
    assert read_clock != star_clock
    apply_snapshot(client, build_snapshot(hub, SyncScope(days=None)))

    for entity, ident, field in [
        (Entity.ENTRY_STATE, "E0", "read"),
        (Entity.ENTRY_STATE, "E0", "starred"),
        (Entity.FEED, feed.id, "title"),
    ]:
        assert repo.field_clock(client, entity, ident, field) == repo.field_clock(
            hub, entity, ident, field,
        )

    delayed = ChangeOp(
        device_id="otro", lamport=read_clock[0] - 1,
        entity=Entity.ENTRY_STATE, entity_id="E0", field="read", value=False,
    )
    assert apply_ops(client, [delayed], record=False).ignored == 1
    assert repo.get_state(client, "E0").read is True

    # Un cambio posterior a «read» pero anterior a «starred» sí puede ganar.
    between = delayed.model_copy(update={"lamport": read_clock[0] + 1})
    assert between.lamport < star_clock[0]
    assert apply_ops(client, [between], record=False).applied == 1
    assert repo.get_state(client, "E0").read is False
    assert repo.get_state(client, "E0").starred is True


def test_relojes_de_articulos_fuera_del_ambito_no_viajan(nodes):
    hub, _, feed = nodes
    repo.set_starred(hub, ["E0"], True)
    foto = build_snapshot(hub, SyncScope(days=7, include_unread=False))
    assert {e["id"] for e in foto["entries"]} == {"E0"}
    clocks = foto["field_clocks"]
    assert any(c["entity_id"] == feed.id for c in clocks)
    assert {c["entity_id"] for c in clocks if c["entity"] == "entry_state"} == {"E0"}


def test_etiqueta_quitada_no_reaparece_tras_el_snapshot(nodes):
    hub, client, _ = nodes
    tag = repo.get_or_create_tag(hub, "investigación")
    repo.tag_entry(hub, "E0", tag.id)
    repo.tag_entry(hub, "E0", tag.id, remove=True)
    repo.tag_entry(hub, "E1", tag.id)
    repo.set_starred(hub, ["E0"], True)
    foto = build_snapshot(hub, SyncScope(days=7, include_unread=False))
    apply_snapshot(client, foto)
    ident = f"E0:{tag.id}"
    clock = repo.field_clock(hub, Entity.ENTRY_TAG, ident, "deleted")
    assert repo.field_clock(client, Entity.ENTRY_TAG, ident, "deleted") == clock
    assert repo.field_clock(client, Entity.ENTRY_TAG, f"E1:{tag.id}", "deleted") is None
    old = ChangeOp(
        device_id="otro", lamport=clock[0] - 1, entity=Entity.ENTRY_TAG,
        entity_id=ident, field="deleted", value=False,
    )
    assert apply_ops(client, [old], record=False).ignored == 1
    assert repo.entry_tags(client, "E0") == []


def test_fallo_al_importar_revierte_datos_relojes_y_cursor(nodes):
    hub, client, _ = nodes
    foto = build_snapshot(hub, SyncScope(days=None, include_unread=False,
                                         include_starred=False))
    foto["state"][-1]["entry_id"] = "no-existe"
    with pytest.raises(sqlite3.IntegrityError):
        apply_snapshot(client, foto)
    assert repo.list_feeds(client) == []
    assert client.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert client.execute("SELECT COUNT(*) FROM field_clock").fetchone()[0] == 0
    assert client.execute("SELECT last_pull_seq FROM node").fetchone()[0] == 0
    assert not client.in_transaction


def test_importacion_respeta_la_transaccion_del_llamador(nodes):
    hub, client, _ = nodes
    client.execute("BEGIN")
    apply_snapshot(client, build_snapshot(hub, SyncScope()))
    assert client.in_transaction
    assert repo.list_feeds(client)
    client.execute("ROLLBACK")
    assert repo.list_feeds(client) == []


def test_snapshot_con_reloj_invalido_revierte_toda_la_importacion(nodes):
    hub, client, _ = nodes
    foto = build_snapshot(hub, SyncScope(days=None))
    foto["field_clocks"][-1]["lamport"] = None
    with pytest.raises(sqlite3.IntegrityError):
        apply_snapshot(client, foto)
    assert repo.list_feeds(client) == []
    assert client.execute("SELECT COUNT(*) FROM field_clock").fetchone()[0] == 0
    assert client.execute("SELECT last_pull_seq FROM node").fetchone()[0] == 0


def test_foto_anterior_sin_relojes_sigue_siendo_importable(nodes):
    hub, client, _ = nodes
    foto = build_snapshot(hub, SyncScope(days=None))
    del foto["field_clocks"]
    apply_snapshot(client, foto)
    assert repo.get_state(client, "E0").read is True


@pytest.mark.parametrize("size", [0, -1])
def test_tamano_de_trozo_invalido_se_rechaza(nodes, size):
    hub, _, _ = nodes
    with pytest.raises(ValueError, match="positivo"):
        list(iter_snapshot_chunks(hub, SyncScope(), chunk=size))


def test_snapshot_por_trozos_respeta_limite_y_solo_confirma_al_final(nodes):
    hub, client, _ = nodes
    scope = SyncScope(days=None, include_unread=False, include_starred=False, max_entries=3)
    complete = build_snapshot(hub, scope)
    chunks = list(iter_snapshot_chunks(hub, scope, chunk=2))
    assert [c["chunk"] for c in chunks] == list(range(len(chunks)))
    assert {e["id"] for c in chunks for e in c.get("entries", [])} == {
        e["id"] for e in complete["entries"]
    }
    assert sum(len(c.get("entries", [])) for c in chunks) == 3
    for part in chunks[:-1]:
        apply_snapshot(client, part)
        assert client.execute("SELECT last_pull_seq FROM node").fetchone()[0] == 0
    apply_snapshot(client, chunks[-1])
    assert client.execute("SELECT last_pull_seq FROM node").fetchone()[0] == complete["cursor"]
    assert client.execute("SELECT lamport FROM node").fetchone()[0] >= complete["server_lamport"]
    assert client.execute("SELECT COUNT(*) FROM field_clock").fetchone()[0] == len(
        complete["field_clocks"],
    )


def test_snapshot_no_mezcla_escrituras_concurrentes(nodes):
    hub, _, _ = nodes
    # Cada yield permite que otra conexión escriba mientras se construye la foto.
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    other = open_db(path)
    parts = iter_snapshot_chunks(hub, SyncScope(days=None), chunk=2)
    first = next(parts)
    try:
        repo.set_read(other, ["E0"], False)
        tail = list(parts)
        state = next(s for p in tail for s in p.get("state", []) if s["entry_id"] == "E0")
        assert state["read"] == 1
        ops, _, _ = repo.changes_since(hub, first["cursor"])
        assert any(op.entity_id == "E0" and op.value is False for op in ops)
    finally:
        parts.close()
        other.close()
    assert not hub.in_transaction


def test_cerrar_el_generador_libera_la_transaccion(nodes):
    hub, _, _ = nodes
    parts = iter_snapshot_chunks(hub, SyncScope())
    next(parts)
    parts.close()
    assert not hub.in_transaction
