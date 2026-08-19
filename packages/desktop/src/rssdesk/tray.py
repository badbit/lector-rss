"""Icono de bandeja y notificaciones nativas.

Aviso conocido: en GNOME con Wayland la bandeja necesita la extensión
AppIndicator; en KDE funciona de forma nativa. Si el sistema no ofrece bandeja,
la aplicación sigue siendo usable: solo se pierde el icono.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


def icono_con_contador(sin_leer: int) -> QIcon:
    """Icono generado al vuelo con el número de artículos sin leer."""
    tam = 64
    pixmap = QPixmap(tam, tam)
    pixmap.fill(QColor(0, 0, 0, 0))
    pintor = QPainter(pixmap)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
    pintor.setBrush(QColor("#e8663d") if sin_leer else QColor("#8a8a8a"))
    pintor.setPen(QColor(0, 0, 0, 0))
    pintor.drawEllipse(2, 2, tam - 4, tam - 4)
    pintor.setPen(QColor("white"))
    fuente = pintor.font()
    fuente.setBold(True)
    fuente.setPixelSize(30 if sin_leer < 100 else 24)
    pintor.setFont(fuente)
    texto = "" if not sin_leer else ("99+" if sin_leer > 99 else str(sin_leer))
    pintor.drawText(pixmap.rect(), 0x0084, texto)  # Qt.AlignCenter
    pintor.end()
    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    mostrar_ventana = Signal()
    refrescar = Signal()
    salir = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(icono_con_contador(0))
        self.setToolTip("Lector RSS")

        menu = QMenu()
        accion_abrir = QAction("Mostrar la ventana", menu)
        accion_abrir.triggered.connect(self.mostrar_ventana.emit)
        accion_refrescar = QAction("Actualizar ahora", menu)
        accion_refrescar.triggered.connect(self.refrescar.emit)
        accion_salir = QAction("Salir", menu)
        accion_salir.triggered.connect(self.salir.emit)
        menu.addAction(accion_abrir)
        menu.addAction(accion_refrescar)
        menu.addSeparator()
        menu.addAction(accion_salir)
        self.setContextMenu(menu)

        self.activated.connect(self._al_activar)

    def _al_activar(self, motivo) -> None:
        if motivo == QSystemTrayIcon.ActivationReason.Trigger:
            self.mostrar_ventana.emit()

    def actualizar_contador(self, sin_leer: int) -> None:
        self.setIcon(icono_con_contador(sin_leer))
        self.setToolTip(f"Lector RSS — {sin_leer} sin leer" if sin_leer else "Lector RSS — al día")

    def avisar(self, titulo: str, cuerpo: str) -> None:
        if QSystemTrayIcon.supportsMessages():
            self.showMessage(titulo, cuerpo, icono_con_contador(0), 8000)


def hay_bandeja() -> bool:
    return QApplication.instance() is not None and QSystemTrayIcon.isSystemTrayAvailable()
