"""Identidad visual y vistas importadas en el escritorio."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from rsscore import repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.models import Entry, Feed
from rsscore.rules.smart import SavedSearch, SavedSearchFilter, save_saved_search
from rssdesk.icons import app_icon
from rssdesk.main import MainWindow
from rssdesk.models import ROL_TIPO
from rssdesk.tray import icono_con_contador


def test_icon_renders_and_smart_folder_selects_its_articles(tmp_path):
    app = QApplication.instance() or QApplication([])
    assert not app_icon().pixmap(64, 64).isNull()
    assert not icono_con_contador(0).pixmap(64, 64).isNull()
    conn = open_db(tmp_path / "ui.db")
    feed = repo.add_feed(conn, Feed(url="https://example.org/feed"))
    entry = Entry(feed_id=feed.id, guid_hash="a", content_hash="b", title="Prueba")
    repo.insert_entry(conn, entry)
    save_saved_search(conn, SavedSearch(name="Cine · Inoreader",
                                       filter=SavedSearchFilter(feed_ids=[feed.id])))
    window = MainWindow(conn, Config())
    try:
        model = window.modelo_arbol
        group = model.index(model.rowCount() - 1, 0)
        index = model.index(0, 0, group)
        assert model.data(index, ROL_TIPO) == "inteligente"
        window._al_elegir_origen(index)
        assert window.modelo_lista.rowCount() == 1
        assert window.modelo_lista.entrada(0).id == entry.id
        assert not window.windowIcon().isNull()
    finally:
        window.stop.set()
        window.hide()
        window.deleteLater()
        app.processEvents()
        conn.close()
