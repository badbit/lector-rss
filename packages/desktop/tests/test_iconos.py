"""Íconos de acciones, tooltips y aspecto de la barra de herramientas."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QToolButton
from rsscore.config import Config
from rsscore.db import open_db
from rssdesk.icons import ACTION_ICONS_DIR, COLOR_ICONS_DIR, action_icon
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


def test_el_tema_cambia_todas_las_acciones_y_se_guarda(crear_ventana):
    ventana = crear_ventana()
    ventana.config_path.write_text(
        "device_name: pruebas\ndesktop:\n  toolbar_style: icons\n", encoding="utf-8"
    )
    acciones = ventana._acciones_con_icono + list(ventana.bandeja._acciones_con_icono)
    originales = [a.icon().pixmap(24, 24).toImage() for a, _ in acciones]
    color = next(a for a in ventana.temas_iconos.actions() if a.data() == "color")
    color.trigger()
    assert color.isChecked()
    assert sum(a.isChecked() for a in ventana.temas_iconos.actions()) == 1
    for (accion, nombre), original in zip(acciones, originales, strict=True):
        actual = accion.icon().pixmap(24, 24).toImage()
        assert actual != original, nombre
        assert actual == action_icon(nombre, "color").pixmap(24, 24).toImage()
    data = yaml.safe_load(ventana.config_path.read_text(encoding="utf-8"))
    assert data == {
        "device_name": "pruebas",
        "desktop": {"toolbar_style": "icons", "icon_theme": "color"},
    }
    otra = crear_ventana(Config.load(ventana.config_path))
    assert otra.temas_iconos.checkedAction().data() == "color"
    assert otra.barra.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    for accion, nombre in otra._acciones_con_icono + list(otra.bandeja._acciones_con_icono):
        assert accion.icon().pixmap(24, 24).toImage() == (
            action_icon(nombre, "color").pixmap(24, 24).toImage()
        )
    mono = next(a for a in ventana.temas_iconos.actions() if a.data() == "monochrome")
    mono.trigger()
    for (accion, _), original in zip(acciones, originales, strict=True):
        assert accion.icon().pixmap(24, 24).toImage() == original
    assert Config.load(ventana.config_path).desktop.icon_theme == "monochrome"


def test_todos_los_iconos_color_estan_empaquetados_y_se_dibujan(app, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for path in ACTION_ICONS_DIR.glob("*.svg"):
        icon = action_icon(path.stem, "color")
        assert (COLOR_ICONS_DIR / f"{path.stem}.svg").is_file()
        for size in (16, 24, 48):
            for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled, QIcon.Mode.Selected):
                img = icon.pixmap(size, size, mode).toImage()
                assert not img.isNull()
                assert any(
                    img.pixelColor(x, y).alpha() for x in range(size) for y in range(size)
                )
    img = action_icon("refresh-cw", "color").pixmap(24, 24).toImage()
    assert any(img.pixelColor(x, y).saturation() > 80 for x in range(24) for y in range(24))


def test_color_recurre_a_monocromo_si_falta_un_recurso(app, tmp_path, monkeypatch):
    monkeypatch.setattr("rssdesk.icons.COLOR_ICONS_DIR", tmp_path)
    assert action_icon("refresh-cw", "color").pixmap(24, 24).toImage() == (
        action_icon("refresh-cw").pixmap(24, 24).toImage()
    )
    assert action_icon("no-existe", "color").isNull()
