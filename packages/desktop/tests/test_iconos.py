"""Íconos de acciones, tooltips y aspecto de la barra de herramientas."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QToolButton
from rsscore.config import Config
from rsscore.db import open_db
from rssdesk.icons import action_icon
from rssdesk.main import MainWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def crear_ventana(app, tmp_path):
    abiertas = []

    def crear(cfg: Config | None = None) -> MainWindow:
        conn = open_db(tmp_path / f"ui{len(abiertas)}.db")
        ventana = MainWindow(conn, cfg or Config(), tmp_path / "config.yaml")
        abiertas.append((ventana, conn))
        return ventana

    yield crear
    for ventana, conn in abiertas:
        ventana.stop.set()
        ventana.bandeja.hide()
        ventana.hide()
        ventana.deleteLater()
        conn.close()
    app.processEvents()


def test_los_iconos_toman_el_color_de_texto_de_la_paleta(app):
    original = app.palette()
    try:
        for texto in ("#101010", "#dadada"):  # tema claro y tema oscuro
            paleta = QPalette(original)
            paleta.setColor(QPalette.ColorRole.WindowText, QColor(texto))
            app.setPalette(paleta)
            imagen = action_icon("refresh-cw").pixmap(48, 48).toImage()
            trazo = {
                imagen.pixelColor(x, y).name()
                for x in range(48)
                for y in range(48)
                if imagen.pixelColor(x, y).alpha() == 255
            }
            assert trazo == {texto}
    finally:
        app.setPalette(original)
    assert action_icon("no-existe").isNull()


def test_barra_y_menus_llevan_iconos_y_ayuda(crear_ventana):
    ventana = crear_ventana()
    botones = [
        a
        for a in ventana.barra.actions()
        if isinstance(ventana.barra.widgetForAction(a), QToolButton)
    ]
    assert len(botones) == 6
    for boton in botones:
        assert not boton.icon().isNull(), boton.text()
        assert boton.statusTip() and boton.statusTip() in boton.toolTip()
    actualizar = next(a for a in botones if "Actualizar" in a.text())
    assert "(F5)" in actualizar.toolTip()

    for superior in ventana.menuBar().actions():
        for accion in superior.menu().actions():
            if not accion.isSeparator():
                assert not accion.icon().isNull(), accion.text()


def test_el_aspecto_de_la_barra_se_elige_y_se_guarda(crear_ventana):
    ventana = crear_ventana()
    ventana.config_path.write_text("device_name: pruebas\n", encoding="utf-8")
    assert ventana.barra.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon

    solo_iconos = next(a for a in ventana.estilos_barra.actions() if a.data() == "icons")
    solo_iconos.trigger()

    assert ventana.barra.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert solo_iconos.isChecked()
    data = yaml.safe_load(ventana.config_path.read_text(encoding="utf-8"))
    assert data == {"device_name": "pruebas", "desktop": {"toolbar_style": "icons"}}

    otra = crear_ventana(Config.load(ventana.config_path))
    assert otra.barra.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly


def test_solo_texto_desde_la_configuracion(crear_ventana):
    ventana = crear_ventana(Config(desktop={"toolbar_style": "text"}))
    assert ventana.barra.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextOnly
