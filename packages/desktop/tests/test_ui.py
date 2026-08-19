"""Pruebas de la interfaz de escritorio, en modo offscreen.

No se prueba el aspecto sino el comportamiento: que el árbol refleje la jerarquía
con sus contadores, que la lista pagine en vez de cargarlo todo y que las
acciones de lectura lleguen de verdad a la base de datos.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from rsscore import repo
from rsscore.db import open_db
from rsscore.ids import hash_content, hash_guid, now_ms
from rsscore.models import Entry, EntrySelection, Feed, Folder
from rssdesk.models import PAGINA, ROL_TIPO, EntryListModel, FeedTreeModel


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "ui.db", device_name="ui")
    dev = repo.upsert_folder(c, Folder(name="Dev"))
    sub = repo.upsert_folder(c, Folder(name="Rust", parent_id=dev.id))
    f1 = repo.add_feed(c, Feed(url="https://a.org/f", title="Feed A", folder_id=dev.id))
    f2 = repo.add_feed(c, Feed(url="https://b.org/f", title="Feed B", folder_id=sub.id))
    f3 = repo.add_feed(c, Feed(url="https://c.org/f", title="Suelto"))
    for feed, cuantos in ((f1, 3), (f2, 5), (f3, PAGINA + 40)):
        for i in range(cuantos):
            repo.insert_entry(
                c,
                Entry(
                    feed_id=feed.id,
                    guid_hash=hash_guid(feed.id, f"g{i}"),
                    content_hash=hash_content(f"{feed.id}{i}"),
                    title=f"Artículo {i} de {feed.title}",
                    published_at=now_ms() - i * 60_000,
                    body_text="cuerpo",
                ),
            )
    return c


def test_arbol_refleja_la_jerarquia_y_suma_contadores(app, conn):
    modelo = FeedTreeModel(conn)
    nombres = []

    def recorrer(padre, profundidad=0):
        for fila in range(modelo.rowCount(padre)):
            idx = modelo.index(fila, 0, padre)
            nombres.append((profundidad, modelo.data(idx), modelo.data(idx, ROL_TIPO)))
            recorrer(idx, profundidad + 1)

    recorrer(modelo.index(-1, -1))  # raíz
    textos = [n for _, n, _ in nombres]

    assert any("Dev" in t for t in textos)
    assert any("Rust" in t for t in textos), "las carpetas anidadas deben aparecer"
    # La carpeta Dev suma lo suyo (3) más lo de su subcarpeta Rust (5).
    dev = next(t for t in textos if t.startswith("Dev"))
    assert "(8)" in dev, f"la carpeta debe sumar sus descendientes, salió {dev}"


def test_la_lista_pagina_en_vez_de_cargarlo_todo(app, conn):
    modelo = EntryListModel(conn)
    assert modelo.rowCount() == PAGINA, "la primera carga debe ser una página, no el archivo entero"
    assert modelo.canFetchMore()

    modelo.fetchMore()
    assert modelo.rowCount() > PAGINA

    while modelo.canFetchMore():
        modelo.fetchMore()
    total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
    assert modelo.rowCount() == total


def test_marcar_leido_llega_a_la_base(app, conn):
    modelo = EntryListModel(conn)
    entrada = modelo.entrada(0)
    assert repo.get_state(conn, entrada.id).read is False

    modelo.marcar([0], leido=True)
    assert repo.get_state(conn, entrada.id).read is True
    assert modelo.estados[entrada.id][0] is True


def test_siguiente_sin_leer_salta_los_ya_leidos(app, conn):
    modelo = EntryListModel(conn)
    modelo.marcar([0, 1, 2], leido=True)
    assert modelo.siguiente_sin_leer(-1) == 3


def test_filtrar_por_carpeta_incluye_las_subcarpetas(app, conn):
    carpeta = repo.folder_by_name(conn, "Dev")
    modelo = EntryListModel(conn)
    modelo.set_seleccion(EntrySelection(folder_ids=[carpeta.id], limit=500))
    # 3 del feed de Dev + 5 del feed de la subcarpeta Rust
    assert modelo.rowCount() == 8
