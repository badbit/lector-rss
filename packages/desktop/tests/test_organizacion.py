"""Interacciones de lectura, menús contextuales y jerarquía de fuentes."""

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog
from rsscore import repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.models import Entry, EntrySelection, Feed, Folder
from rssdesk.main import MainWindow
from rssdesk.models import PAGINA, ROL_SIN_LEER, EntryListModel


@pytest.fixture
def ventana(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    conn = open_db(tmp_path / "ui.db")
    fuente = repo.add_feed(conn, Feed(url="https://ejemplo.test/rss", title="Ejemplo"))
    for i in range(3):
        repo.insert_entry(conn, Entry(feed_id=fuente.id, guid_hash=str(i), content_hash=str(i),
                                      title=f"Noticia {i}", published_at=1000 + i,
                                      body_html=f"<p>Texto {i}</p>"))
    window = MainWindow(conn, Config(), tmp_path / "config.yaml")
    # Los cambios de organización no deben usar servicios reales en estas pruebas.
    monkeypatch.setattr(window, "_lanzar", lambda coro: coro.close())
    window.show()
    app.processEvents()
    yield window
    window._temporizador_lectura.stop()
    window.stop.set()
    window.bandeja.hide()
    window.hide()
    window.deleteLater()
    app.processEvents()
    conn.close()


def elegir_vista(window, vista):
    index = window.modelo_arbol.indice_de("especial", vista)
    window.arbol.setCurrentIndex(index)
    return index


def abrir_contextual(window, fila):
    pos = window.lista.visualRect(window.modelo_lista.index(fila, 1)).center()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                             window.lista.viewport().mapToGlobal(pos))
    QApplication.sendEvent(window.lista.viewport(), event)
    QApplication.processEvents()
    return window._menu_articulos


def pulsar(menu, texto):
    action = next(a for a in menu.actions() if a.text() == texto)
    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
    QApplication.processEvents()


def test_clic_retira_no_leido_sin_marcar_el_siguiente_y_conserva_lectura(ventana):
    index = elegir_vista(ventana, "sin_leer")
    entrada = ventana.modelo_lista.entrada(0)
    pos = ventana.lista.visualRect(ventana.modelo_lista.index(0, 1)).center()
    QTest.mouseClick(ventana.lista.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    QTest.qWait(850)
    assert repo.get_state(ventana.conn, entrada.id).read
    assert ventana.modelo_lista.rowCount() == 2
    assert ventana.articulo.entrada_actual.id == entrada.id
    assert ventana.modelo_arbol.data(index, ROL_SIN_LEER) == 2
    assert ventana.contadores_estado.text() == "Total: 3 · Leídas: 1 · No leídas: 2 · Guardadas: 0"
    QTest.qWait(850)
    assert ventana.modelo_lista.rowCount() == 2
    assert not ventana.lista.currentIndex().isValid()


def test_contextual_afecta_la_fila_pulsada_y_no_la_que_estaba_seleccionada(ventana):
    ventana.lista.selectRow(0)
    primero = ventana.modelo_lista.entrada(0).id
    segundo = ventana.modelo_lista.entrada(1).id
    menu = abrir_contextual(ventana, 1)
    textos = {a.text() for a in menu.actions()}
    assert {"Enviar a Obsidian", "Enviar a Kindle", "Marcar como leído",
            "Marcar como no leído", "Mover fuente a carpeta…", "Guardar"} <= textos
    pulsar(menu, "Guardar")
    assert repo.get_state(ventana.conn, segundo).starred
    assert not repo.get_state(ventana.conn, primero).starred
    assert "Guardadas: 1" in ventana.contadores_estado.text()
    QTest.qWait(850)
    assert not repo.get_state(ventana.conn, segundo).read
    menu = abrir_contextual(ventana, 1)
    pulsar(menu, "Marcar como leído")
    assert repo.get_state(ventana.conn, segundo).read
    menu = abrir_contextual(ventana, 1)
    pulsar(menu, "Marcar como no leído")
    QTest.qWait(850)
    assert not repo.get_state(ventana.conn, segundo).read


def test_quitar_guardado_actualiza_la_vista(ventana):
    entry = ventana.modelo_lista.entrada(0)
    repo.set_starred(ventana.conn, [entry.id], True)
    elegir_vista(ventana, "guardados")
    pulsar(abrir_contextual(ventana, 0), "Quitar de guardados")
    assert ventana.modelo_lista.rowCount() == 0
    assert "Guardadas: 0" in ventana.contadores_estado.text()


@pytest.mark.parametrize("texto,metodo", [
    ("Enviar a Obsidian", "exportar_obsidian"), ("Enviar a Kindle", "enviar_kindle"),
])
def test_exportacion_contextual_conserva_el_destino_aunque_cambie_la_seleccion(
    ventana, monkeypatch, texto, metodo,
):
    recibidos = []
    tareas = []

    async def exportar(ids):
        recibidos.extend(ids)
        return [] if metodo == "exportar_obsidian" else "Enviado"

    monkeypatch.setattr(ventana.backend, metodo, exportar)
    monkeypatch.setattr(ventana, "_lanzar", tareas.append)
    ident = ventana.modelo_lista.entrada(1).id
    pulsar(abrir_contextual(ventana, 1), texto)
    ventana.lista.selectRow(2)
    asyncio.run(tareas.pop())
    assert recibidos == [ident]


def test_crear_subcarpeta_y_mover_fuente_desde_entrada(ventana, monkeypatch):
    parent = repo.upsert_folder(ventana.conn, Folder(name="Cine"))
    ventana._recargar_arbol()
    ventana.arbol.setCurrentIndex(ventana.modelo_arbol.indice_de("carpeta", parent.id))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Welles", True))
    ventana._nueva_carpeta()
    sub = next(f for f in repo.list_folders(ventana.conn) if f.name == "Welles")
    assert sub.parent_id == parent.id
    elegir_vista(ventana, "todos")

    def elegir(*args, **kwargs):
        assert "Cine / Welles" in args[3]
        return "Cine / Welles", True

    monkeypatch.setattr(QInputDialog, "getItem", elegir)
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    pulsar(abrir_contextual(ventana, 0), "Mover fuente a carpeta…")
    assert repo.get_feed(ventana.conn, feed_id).folder_id == sub.id
    assert ventana.conn.execute("SELECT COUNT(*) FROM entries WHERE feed_id=?", (feed_id,)
                               ).fetchone()[0] == 3
    index = ventana.modelo_arbol.indice_de("feed", feed_id)
    assert ventana.modelo_arbol.parent(index) == ventana.modelo_arbol.indice_de("carpeta", sub.id)


def test_mover_carpeta_impide_ciclos_y_emite_cambio(ventana):
    conn = ventana.conn
    parent = repo.upsert_folder(conn, Folder(name="Padre"))
    child = repo.upsert_folder(conn, Folder(name="Hija", parent_id=parent.id))
    with pytest.raises(ValueError):
        repo.move_folder(conn, parent.id, child.id)
    with pytest.raises(ValueError):
        repo.move_folder(conn, parent.id, parent.id)
    repo.move_folder(conn, child.id, None)
    assert repo.get_folder(conn, child.id).parent_id is None
    change = conn.execute("SELECT field,value_json FROM change_log WHERE entity_id=? "
                          "ORDER BY seq DESC LIMIT 1", (child.id,)).fetchone()
    assert tuple(change) == ("parent_id", "null")


def test_no_leidos_pagina_sin_omitir_entradas_tras_retirar_filas(ventana):
    conn = ventana.conn
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    for i in range(PAGINA):
        repo.insert_entry(conn, Entry(feed_id=feed_id, guid_hash=f"extra{i}",
                                      content_hash=f"extra{i}", title=f"Extra {i}"))
    model = EntryListModel(conn)
    model.set_seleccion(EntrySelection(unread_only=True))
    primero = model.entrada(0).id
    model.marcar([0], leido=True)
    while model.canFetchMore():
        model.fetchMore()
    ids = [e.id for e in model.entradas]
    assert len(ids) == len(set(ids)) == PAGINA + 2
    assert primero not in ids
    ventana.modelo_lista.set_seleccion(EntrySelection(unread_only=True))
    ventana._marcar_todo_leido()
    assert ventana.modelo_lista.rowCount() == 0
    assert repo.unread_counts(conn) == {}


def test_cambiar_de_origen_cancela_la_lectura_pendiente(ventana):
    ventana.lista.selectRow(0)
    ident = ventana.modelo_lista.entrada(0).id
    elegir_vista(ventana, "guardados")
    QTest.qWait(850)
    assert not repo.get_state(ventana.conn, ident).read


def test_mover_carpeta_desde_su_menu_contextual(ventana, monkeypatch):
    padre = repo.upsert_folder(ventana.conn, Folder(name="Cine"))
    hija = repo.upsert_folder(ventana.conn, Folder(name="Welles", parent_id=padre.id))
    destino = repo.upsert_folder(ventana.conn, Folder(name="Archivo"))
    ventana._recargar_arbol()
    index = ventana.modelo_arbol.indice_de("carpeta", padre.id)
    pos = ventana.arbol.visualRect(index).center()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                             ventana.arbol.viewport().mapToGlobal(pos))
    QApplication.sendEvent(ventana.arbol.viewport(), event)
    QApplication.processEvents()

    def elegir(*args, **kwargs):
        assert args[3] == ["Sin carpeta (raíz)", "Archivo"]
        return "Archivo", True

    monkeypatch.setattr(QInputDialog, "getItem", elegir)
    pulsar(ventana._menu_fuentes, "Mover carpeta…")
    assert repo.get_folder(ventana.conn, padre.id).parent_id == destino.id
    assert repo.get_folder(ventana.conn, hija.id).parent_id == padre.id


def test_contadores_del_archivo_vacio_y_mensajes_de_estado(tmp_path):
    app = QApplication.instance() or QApplication([])
    conn = open_db(tmp_path / "empty.db")
    window = MainWindow(conn, Config(), tmp_path / "config.yaml")
    try:
        window.show()
        window.statusBar().showMessage("Actualizando…")
        app.processEvents()
        assert window.contadores_estado.isVisible()
        assert window.contadores_estado.text() == (
            "Total: 0 · Leídas: 0 · No leídas: 0 · Guardadas: 0"
        )
    finally:
        window.bandeja.hide()
        window.hide()
        window.deleteLater()
        app.processEvents()
        conn.close()
