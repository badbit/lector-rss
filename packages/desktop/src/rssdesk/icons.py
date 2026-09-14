"""Íconos empaquetados, independientes del tema del escritorio y del directorio actual.

Los de las acciones son de Lucide y dibujan con ``currentColor``: se pintan en el
momento con el color de texto de la paleta vigente. Un trazo negro fijo
desaparecería en un tema oscuro, y así también siguen al tema si éste cambia con
la aplicación abierta.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

ASSETS_DIR = Path(__file__).parent / "assets"
ICON_PATH = ASSETS_DIR / "org.badbit.LectorRSS.svg"
ACTION_ICONS_DIR = ASSETS_DIR / "iconos"


def app_icon() -> QIcon:
    return QIcon(str(ICON_PATH))


def action_icon(name: str) -> QIcon:
    """Ícono de Lucide (``assets/iconos/<name>.svg``) teñido con la paleta."""
    return QIcon(_LucideIconEngine(name))


@cache
def _renderer(name: str, color: str) -> QSvgRenderer:
    path = ACTION_ICONS_DIR / f"{name}.svg"
    svg = path.read_bytes().replace(b"currentColor", color.encode()) if path.is_file() else b""
    return QSvgRenderer(QByteArray(svg))


def _color_for(mode: QIcon.Mode) -> str:
    palette = QGuiApplication.palette()
    if mode == QIcon.Mode.Disabled:
        color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    elif mode == QIcon.Mode.Selected:
        color = palette.color(QPalette.ColorRole.HighlightedText)
    else:
        # Active incluido: los botones de la barra lo usan al pasar el ratón
        # sobre un fondo apenas resaltado, no sobre el color de selección.
        color = palette.color(QPalette.ColorRole.WindowText)
    return color.name()


class _LucideIconEngine(QIconEngine):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        renderer = _renderer(self.name, _color_for(mode))
        if not renderer.isValid():
            return
        side = min(rect.width(), rect.height())
        square = QRectF(0, 0, side, side)
        square.moveCenter(QRectF(rect).center())
        renderer.render(painter, square)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
        painter.end()
        return pixmap

    def isNull(self) -> bool:
        return not _renderer(self.name, _color_for(QIcon.Mode.Normal)).isValid()

    def clone(self) -> QIconEngine:
        return _LucideIconEngine(self.name)
