"""Pruebas de la sincronización.

Simulan varios dispositivos y un hub sin usar la red: se llama directamente a
`apply_ops` con las operaciones que cada nodo tiene en su cola. Lo que se
comprueba son las propiedades que hacen que el sistema converja: idempotencia,
conmutatividad y determinismo del conflicto.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from rsscore import repo
from rsscore.db import open_db
from rsscore.ids import hash_content, hash_guid, now_ms
from rsscore.models import Entry, Feed, SyncScope
from rsscore.sync import (
    apply_ops,
    apply_snapshot,
    build_snapshot,
    compact_change_log,
    filter_ops_for_scope,
    min_client_seq,
    replay_pending,
)


class Nodo:
    """Un dispositivo (o el hub) con su base y su cursor de lectura."""

    def __init__(self, nombre: str, tmp: Path):
        self.nombre = nombre
        self.path = tmp / f"{nombre}.db"
        self.conn = open_db(self.path, device_name=nombre)
        self.cursor = 0

    @property
    def device_id(self) -> str:
        return self.conn.execute("SELECT device_id FROM node WHERE id = 1").fetchone()["device_id"]

    def pendientes(self):
        """Vacía la cola de subida y devuelve sus operaciones."""
        lote = repo.outbox_batch(self.conn, 10_000)
        repo.outbox_clear(self.conn, [i for i, _ in lote])
        return [op for _, op in lote]

    def recibir(self, ops, *, record=True):
        return apply_ops(self.conn, ops, record=record)

    def delta_para(self, otro: Nodo):
        """Lo que el hub le debe a un cliente: todo menos lo que él mismo mandó."""
        ops, cursor, _ = repo.changes_since(self.conn, otro.cursor, 10_000)
        otro.cursor = cursor
        return [op for op in ops if op.device_id != otro.device_id]

    def estado(self, entry_id):
        return repo.get_state(self.conn, entry_id)


@pytest.fixture
def escenario():
    """Un hub y dos dispositivos que ya comparten un feed y un artículo."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        hub, a, b = Nodo("hub", tmp), Nodo("portatil", tmp), Nodo("movil", tmp)
        feed = Feed(url="https://ejemplo.org/feed", title="Ejemplo")
        entry_id = None
        for nodo in (hub, a, b):
            repo.add_feed(nodo.conn, feed.model_copy(), track=False)
            e = Entry(
                id="ENTRADA1", feed_id=feed.id, guid_hash=hash_guid(feed.id, "g1"),
                content_hash=hash_content("x"), title="Artículo de prueba",
                published_at=now_ms(),
            )
            repo.insert_entry(nodo.conn, e)
            entry_id = e.id
        # El estado inicial no es un cambio que haya que sincronizar.
        for nodo in (hub, a, b):
            nodo.conn.execute("DELETE FROM change_log")
            nodo.conn.execute("DELETE FROM outbox")
            nodo.cursor = 0
        yield hub, a, b, entry_id, feed


def sincronizar(hub: Nodo, *clientes: Nodo):
    """Un ciclo completo: todos suben, luego todos bajan."""
    for c in clientes:
        hub.recibir(c.pendientes(), record=True)
    for c in clientes:
        c.recibir(hub.delta_para(c), record=False)


# ---------------------------------------------------------------- convergencia
def test_convergencia_con_conflicto(escenario):
    """Dos dispositivos marcan el MISMO artículo de forma distinta sin conexión."""
    hub, a, b, entry_id, _ = escenario

    repo.set_read(a.conn, [entry_id], True)       # el portátil lo marca leído
    repo.set_read(b.conn, [entry_id], False)      # el móvil lo deja sin leer
    # Adelantamos el reloj del móvil para que su escritura sea la posterior.
    b.conn.execute("UPDATE node SET lamport = 50 WHERE id = 1")
    repo.set_read(b.conn, [entry_id], False)

    sincronizar(hub, a, b)
    sincronizar(hub, a, b)   # segunda vuelta: nada debe cambiar ya

    estados = {n.nombre: n.estado(entry_id).read for n in (hub, a, b)}
    assert len(set(estados.values())) == 1, f"no convergieron: {estados}"
    assert estados["portatil"] is False, "debe ganar la escritura con lamport mayor"


def test_idempotencia(escenario):
    hub, a, b, entry_id, _ = escenario
    repo.set_starred(a.conn, [entry_id], True)
    ops = a.pendientes()

    primero = hub.recibir(ops)
    segundo = hub.recibir(ops)          # exactamente el mismo lote otra vez

    assert primero.applied == len(ops)
    assert segundo.applied == 0, "reaplicar el mismo lote no debe volver a escribir"
    assert hub.estado(entry_id).starred is True


def test_conmutatividad(escenario):
    """El orden de llegada no puede cambiar el resultado."""
    hub, a, b, entry_id, _ = escenario
    repo.set_read(a.conn, [entry_id], True)
    repo.set_starred(a.conn, [entry_id], True)
    ops = a.pendientes()

    hub.recibir(ops)
    b.recibir(list(reversed(ops)))      # el mismo lote, al revés

    assert hub.estado(entry_id).read == b.estado(entry_id).read
    assert hub.estado(entry_id).starred == b.estado(entry_id).starred


def test_tombstone_de_etiqueta(escenario):
    """Quitar una etiqueta debe ganar a haberla puesto antes."""
    hub, a, b, entry_id, _ = escenario

    tag = repo.get_or_create_tag(a.conn, "importante")
    repo.tag_entry(a.conn, entry_id, tag.id)
    sincronizar(hub, a, b)
    assert [t.name for t in repo.entry_tags(b.conn, entry_id)] == ["importante"]

    b.conn.execute("UPDATE node SET lamport = 99 WHERE id = 1")
    repo.tag_entry(b.conn, entry_id, tag.id, remove=True)
    sincronizar(hub, a, b)

    for nodo in (hub, a, b):
        assert repo.entry_tags(nodo.conn, entry_id) == [], f"{nodo.nombre} conserva la etiqueta"


def test_operacion_huerfana_se_recupera(escenario):
    """Llega el estado de un artículo que este nodo todavía no ha descargado."""
    hub, a, b, _, feed = escenario

    nueva = Entry(
        id="ENTRADA2", feed_id=feed.id, guid_hash=hash_guid(feed.id, "g2"),
        content_hash=hash_content("y"), title="Todavía no descargada", published_at=now_ms(),
    )
    repo.insert_entry(a.conn, nueva)
    repo.set_read(a.conn, [nueva.id], True)

    resultado = b.recibir(a.pendientes(), record=False)
    assert resultado.pending >= 1, "debería aparcarla, no descartarla"
    assert b.estado(nueva.id) is None

    repo.insert_entry(b.conn, nueva)            # ahora sí llega el artículo
    assert replay_pending(b.conn) >= 1
    assert b.estado(nueva.id).read is True


# ---------------------------------------------------------------- compactación
def test_compactacion_no_rompe_a_un_cliente_rezagado(escenario):
    hub, a, b, entry_id, _ = escenario

    for valor in (True, False, True, False, True):
        repo.set_read(a.conn, [entry_id], valor)
    hub.recibir(a.pendientes())
    b.cursor = 0                                  # el móvil no ha sincronizado aún

    hub.conn.execute(
        "INSERT INTO sync_clients (device_id, name, last_seq, scope_json, last_seen_at) "
        "VALUES (?, 'movil', 0, '{}', ?)",
        (b.device_id, now_ms()),
    )
    keep = min_client_seq(hub.conn)
    assert keep == 0, "con un cliente en el cursor 0 no se puede compactar nada"

    hub.conn.execute("UPDATE sync_clients SET last_seq = 3 WHERE device_id = ?", (b.device_id,))
    b.cursor = 3
    borradas = compact_change_log(hub.conn, keep_seq=min_client_seq(hub.conn))
    assert borradas > 0

    b.recibir(hub.delta_para(b), record=False)
    assert b.estado(entry_id).read == hub.estado(entry_id).read


# --------------------------------------------------------------------- ámbito
def test_ambito_deja_fuera_lo_antiguo_pero_no_lo_guardado(escenario):
    hub, a, b, entry_id, feed = escenario
    antiguo = now_ms() - 400 * 86_400_000

    viejo = Entry(
        id="VIEJA", feed_id=feed.id, guid_hash=hash_guid(feed.id, "old"),
        content_hash=hash_content("old"), title="De hace un año", published_at=antiguo,
    )
    guardado = Entry(
        id="VIEJAGUARDADA", feed_id=feed.id, guid_hash=hash_guid(feed.id, "oldstar"),
        content_hash=hash_content("oldstar"), title="Vieja pero guardada", published_at=antiguo,
    )
    for e in (viejo, guardado):
        repo.insert_entry(hub.conn, e)
    repo.set_read(hub.conn, [viejo.id], True)          # leída y antigua: fuera
    repo.set_starred(hub.conn, [guardado.id], True)    # guardada: entra igual
    repo.set_read(hub.conn, [guardado.id], True)

    scope = SyncScope(days=7, include_starred=True, include_unread=False)
    ops, _, _ = repo.changes_since(hub.conn, 0, 10_000)
    filtradas = filter_ops_for_scope(hub.conn, ops, scope)

    ids = {op.entity_id for op in filtradas}
    assert "VIEJA" not in ids, "un artículo leído de hace un año no debe viajar al móvil"
    assert "VIEJAGUARDADA" in ids, "lo guardado entra en el ámbito aunque sea antiguo"


def test_snapshot_arranca_un_cliente_nuevo(escenario):
    hub, a, b, entry_id, feed = escenario
    repo.set_starred(hub.conn, [entry_id], True)

    with tempfile.TemporaryDirectory() as d:
        nuevo = Nodo("tablet", Path(d))
        foto = build_snapshot(hub.conn, SyncScope(days=30))
        apply_snapshot(nuevo.conn, foto)

        assert len(repo.list_feeds(nuevo.conn)) == 1
        assert nuevo.estado(entry_id).starred is True
        assert nuevo.conn.execute(
            "SELECT last_pull_seq FROM node WHERE id = 1"
        ).fetchone()["last_pull_seq"] == foto["cursor"]
        # El índice de búsqueda se reconstruye: el snapshot no lo trae.
        assert repo.search(nuevo.conn, "prueba")
