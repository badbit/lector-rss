"""Interacciones de lectura, menús contextuales y jerarquía de fuentes."""

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog
from rsscore import repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.models import Entry, EntrySelection, Feed, Folder
from rssdesk.main import MainWindow
from rssdesk.models import PAGINA, ROL_SIN_LEER, EntryListModel, FeedTreeModel


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


def test_doble_clic_abre_la_fila_pulsada(ventana, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    for i, e in enumerate(ventana.modelo_lista.entradas):
        e.url = f"https://ejemplo.test/{i}"
    pos = ventana.lista.visualRect(ventana.modelo_lista.index(1, 1)).center()
    QTest.mouseClick(ventana.lista.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseDClick(ventana.lista.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert opened == ["https://ejemplo.test/1"]


def boton_marcar(window):
    action = next(a for a in window.barra.actions() if a.text() == "Marcar todo como leído")
    QTest.mouseClick(window.barra.widgetForAction(action), Qt.MouseButton.LeftButton)


def test_boton_marca_multiseleccion_no_toda_la_fuente(ventana):
    ventana.lista.selectRow(0)
    ventana.lista.selectionModel().select(ventana.modelo_lista.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    ids = [e.id for e in ventana.modelo_lista.entradas]
    boton_marcar(ventana)
    assert [repo.get_state(ventana.conn, i).read for i in ids] == [True, True, False]
    QTest.qWait(850)
    assert not repo.get_state(ventana.conn, ids[2]).read


def test_boton_una_entrada_marca_su_fuente_incluye_no_cargadas(ventana):
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    other = repo.add_feed(ventana.conn, Feed(url="https://otra.test/rss", title="Otra"))
    other_entry = repo.insert_entry(ventana.conn, Entry(feed_id=other.id, guid_hash="other",
                                                       content_hash="other", title="Otra"))
    for i in range(PAGINA + 5):
        repo.insert_entry(ventana.conn, Entry(feed_id=feed_id, guid_hash=f"b{i}",
                                              content_hash=f"b{i}", title=f"Extra{i}"))
    ventana.lista.selectRow(0)
    boton_marcar(ventana)
    assert repo.unread_counts(ventana.conn).get(feed_id, 0) == 0
    assert not repo.get_state(ventana.conn, other_entry).read


def test_boton_fuente_sin_entradas_seleccionadas(ventana):
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    ventana.arbol.setCurrentIndex(ventana.modelo_arbol.indice_de("feed", feed_id))
    assert not ventana.lista.selectionModel().selectedRows()
    boton_marcar(ventana)
    assert repo.unread_counts(ventana.conn) == {}


def test_arrastrar_fuente_a_carpeta_y_raiz_persiste(ventana):
    folder = repo.upsert_folder(ventana.conn, Folder(name="Cine"))
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    ventana._recargar_arbol()
    model = ventana.modelo_arbol
    source = model.indice_de("feed", feed_id)
    dest = model.indice_de("carpeta", folder.id)
    mime = model.mimeData([source])
    assert model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, 0, dest)
    assert repo.get_feed(ventana.conn, feed_id).folder_id == folder.id
    assert model.indice_de("feed", feed_id).parent().data() == "Cine  (3)"
    assert model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, 0, QModelIndex())
    assert repo.get_feed(ventana.conn, feed_id).folder_id is None
    assert not model.indice_de("feed", feed_id).parent().isValid()


def test_arrastrar_carpeta_impide_ciclos_y_ordena_persistentemente(ventana):
    conn = ventana.conn
    a = repo.upsert_folder(conn, Folder(name="A"))
    b = repo.upsert_folder(conn, Folder(name="B", parent_id=a.id))
    c = repo.upsert_folder(conn, Folder(name="C"))
    ventana._recargar_arbol()
    model = ventana.modelo_arbol
    ia = model.indice_de("carpeta", a.id)
    ib = model.indice_de("carpeta", b.id)
    mime = model.mimeData([ia])
    for target in (ia, ib, model.indice_de("especial", "todos")):
        assert not model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, 0, target)
    ic = model.indice_de("carpeta", c.id)
    assert model.dropMimeData(model.mimeData([ic]), Qt.DropAction.MoveAction,
                              ia.row(), 0, QModelIndex())
    reloaded = FeedTreeModel(conn)
    assert reloaded.indice_de("carpeta", c.id).row() < reloaded.indice_de("carpeta", a.id).row()
    assert repo.get_folder(conn, b.id).parent_id == a.id
    assert not reloaded.canDropMimeData(mime, Qt.DropAction.MoveAction, -1, 0, QModelIndex())


def test_drop_event_del_arbol_mueve_fuente(ventana):
    folder = repo.upsert_folder(ventana.conn, Folder(name="Destino"))
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    ventana._recargar_arbol()
    model = ventana.modelo_arbol
    source = model.indice_de("feed", feed_id)
    dest = model.indice_de("carpeta", folder.id)
    mime = model.mimeData([source])
    pos = ventana.arbol.visualRect(dest).center()
    enter = QDragEnterEvent(pos, Qt.DropAction.MoveAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(ventana.arbol.viewport(), enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(pos), Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(ventana.arbol.viewport(), drop)
    assert drop.isAccepted()
    assert repo.get_feed(ventana.conn, feed_id).folder_id == folder.id


def test_iconos_del_arbol_distinguen_carpeta_y_fuente(ventana):
    folder = repo.upsert_folder(ventana.conn, Folder(name="Carpeta"))
    feed_id = ventana.modelo_lista.entrada(0).feed_id
    ventana._recargar_arbol()
    model = ventana.modelo_arbol
    folder_icon = model.indice_de("carpeta", folder.id).data(Qt.ItemDataRole.DecorationRole)
    feed_icon = model.indice_de("feed", feed_id).data(Qt.ItemDataRole.DecorationRole)
    assert not folder_icon.isNull() and not feed_icon.isNull()
    assert folder_icon.pixmap(24, 24).toImage() != feed_icon.pixmap(24, 24).toImage()
