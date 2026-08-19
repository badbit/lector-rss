"""Ventana principal del lector: tres paneles al estilo de Liferea.

Distribución: suscripciones a la izquierda, lista de artículos arriba a la
derecha y el artículo debajo. Los atajos son los de Liferea para que la memoria
muscular siga sirviendo: `n` salta al siguiente sin leer, la barra espaciadora
avanza y encadena artículos, `Ctrl+A` marca la carpeta entera como leída.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import qasync
from PySide6.QtCore import QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTableView,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from rsscore import repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.models import EntrySelection

from .article import ArticleView
from .models import ROL_ID, ROL_TIPO, EntryListModel, FeedTreeModel
from .tasks import Backend
from .tray import Tray

log = logging.getLogger("rssdesk")


class MainWindow(QMainWindow):
    def __init__(self, conn, cfg: Config) -> None:
        super().__init__()
        self.conn = conn
        self.cfg = cfg
        self.backend = Backend(conn, cfg)
        self.stop = asyncio.Event()
        self._tareas: set[asyncio.Task] = set()
        self.setWindowTitle("Lector RSS")
        self.resize(1280, 820)

        self._construir_paneles()
        self._construir_acciones()
        self._construir_bandeja()
        self.setStatusBar(QStatusBar())
        self._actualizar_contadores()

    # ------------------------------------------------------------- interfaz
    def _construir_paneles(self) -> None:
        self.arbol = QTreeView()
        self.modelo_arbol = FeedTreeModel(self.conn)
        self.arbol.setModel(self.modelo_arbol)
        self.arbol.expandAll()
        self.arbol.setHeaderHidden(False)
        self.arbol.selectionModel().currentChanged.connect(self._al_elegir_origen)

        self.lista = QTableView()
        self.modelo_lista = EntryListModel(self.conn)
        self.lista.setModel(self.modelo_lista)
        self.lista.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.lista.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.lista.verticalHeader().setVisible(False)
        self.lista.setShowGrid(False)
        self.lista.setSortingEnabled(False)
        cabecera = self.lista.horizontalHeader()
        cabecera.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.lista.setColumnWidth(0, 26)
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        cabecera.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.lista.selectionModel().currentRowChanged.connect(self._al_elegir_articulo)

        self.buscador = QLineEdit()
        self.buscador.setPlaceholderText("Buscar en todo el archivo…  (Ctrl+F)")
        self.buscador.setClearButtonEnabled(True)
        self.buscador.returnPressed.connect(self._buscar)

        derecha_arriba = QWidget()
        caja = QVBoxLayout(derecha_arriba)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(2)
        caja.addWidget(self.buscador)
        caja.addWidget(self.lista)

        self.articulo = ArticleView()

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(derecha_arriba)
        vertical.addWidget(self.articulo)
        vertical.setSizes([300, 520])

        horizontal = QSplitter(Qt.Orientation.Horizontal)
        horizontal.addWidget(self.arbol)
        horizontal.addWidget(vertical)
        horizontal.setSizes([280, 1000])
        self.setCentralWidget(horizontal)

    def _construir_acciones(self) -> None:
        barra = QToolBar("Principal")
        barra.setMovable(False)
        self.addToolBar(barra)
        menu_archivo = self.menuBar().addMenu("&Archivo")
        menu_ver = self.menuBar().addMenu("&Ver")
        menu_exportar = self.menuBar().addMenu("&Exportar")

        def accion(texto, atajo, slot, *, en_barra=False, menu=None):
            a = QAction(texto, self)
            if atajo:
                a.setShortcut(QKeySequence(atajo))
            a.triggered.connect(slot)
            self.addAction(a)
            if en_barra:
                barra.addAction(a)
            if menu is not None:
                menu.addAction(a)
            return a

        accion("&Suscribirse…", "Ctrl+N", self._suscribirse, en_barra=True, menu=menu_archivo)
        accion(
            "&Actualizar todo",
            "F5",
            lambda: self._lanzar(self._refrescar(todos=True)),
            en_barra=True,
            menu=menu_archivo,
        )
        accion(
            "&Sincronizar",
            "Ctrl+S",
            lambda: self._lanzar(self._sincronizar()),
            en_barra=True,
            menu=menu_archivo,
        )
        menu_archivo.addSeparator()
        accion("Importar OPML…", None, self._importar_opml, menu=menu_archivo)
        accion("Exportar OPML…", None, self._exportar_opml, menu=menu_archivo)
        menu_archivo.addSeparator()
        accion("&Salir", "Ctrl+Q", self._salir, menu=menu_archivo)

        # Atajos de lectura, calcados de Liferea.
        accion("Siguiente sin leer", "n", self._siguiente_sin_leer, menu=menu_ver)
        accion("Siguiente sin leer (j)", "j", self._siguiente_sin_leer)
        accion("Avanzar", "Space", self._avanzar, menu=menu_ver)
        accion("Alternar leído", "r", lambda: self._alternar("leido"), menu=menu_ver)
        accion("Alternar guardado", "s", lambda: self._alternar("guardado"), menu=menu_ver)
        accion("Marcar todo como leído", "Ctrl+A", self._marcar_todo_leido, menu=menu_ver)
        accion("Abrir en el navegador", "Ctrl+O", self._abrir_en_navegador, menu=menu_ver)
        accion("Buscar", "Ctrl+F", lambda: self.buscador.setFocus(), menu=menu_ver)

        accion(
            "A &Obsidian",
            "Ctrl+E",
            lambda: self._lanzar(self._exportar_obsidian()),
            en_barra=True,
            menu=menu_exportar,
        )
        accion(
            "Al &Kindle",
            "Ctrl+K",
            lambda: self._lanzar(self._enviar_kindle()),
            en_barra=True,
            menu=menu_exportar,
        )
        accion(
            "Generar &revista EPUB",
            "Ctrl+M",
            lambda: self._lanzar(self._revista()),
            en_barra=True,
            menu=menu_exportar,
        )

        self.etiqueta_estado = QLabel("")
        barra.addSeparator()
        barra.addWidget(self.etiqueta_estado)

    def _construir_bandeja(self) -> None:
        self.bandeja = Tray(self)
        self.bandeja.mostrar_ventana.connect(self._mostrar)
        self.bandeja.refrescar.connect(lambda: self._lanzar(self._refrescar(todos=True)))
        self.bandeja.salir.connect(self._salir)
        self.bandeja.show()

    # -------------------------------------------------------------- eventos
    def closeEvent(self, event) -> None:
        """Cerrar la ventana la esconde en la bandeja; se sale con Ctrl+Q."""
        if self.bandeja.isVisible():
            self.hide()
            event.ignore()
        else:
            event.accept()

    def _mostrar(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _salir(self) -> None:
        self.stop.set()
        self.bandeja.hide()
        QApplication.instance().quit()

    def _al_elegir_origen(self, index: QModelIndex, _anterior=None) -> None:
        if not index.isValid():
            return
        tipo = self.modelo_arbol.data(index, ROL_TIPO)
        ident = self.modelo_arbol.data(index, ROL_ID)
        seleccion = EntrySelection(limit=200)
        match tipo:
            case "feed":
                seleccion.feed_ids = [ident]
            case "carpeta":
                seleccion.folder_ids = [ident]
            case "especial":
                if ident == "sin_leer":
                    seleccion.unread_only = True
                elif ident == "guardados":
                    seleccion.starred_only = True
            case _:
                return
        self.buscador.clear()
        self.modelo_lista.set_seleccion(seleccion)
        self.articulo.limpiar()

    def _al_elegir_articulo(self, index: QModelIndex, _anterior=None) -> None:
        entrada = self.modelo_lista.entrada(index.row())
        if entrada is None:
            return
        completa = repo.get_entry(self.conn, entrada.id, with_body=True)
        if completa is None:
            return
        feed = repo.get_feed(self.conn, completa.feed_id)
        etiquetas = [t.name for t in repo.entry_tags(self.conn, completa.id)]
        self.articulo.mostrar(completa, feed.display_title if feed else "", etiquetas)
        # Marcar como leído tras un momento, no al pasar de largo con las flechas.
        QTimer.singleShot(700, lambda fila=index.row(): self._marcar_leido_si_sigue(fila))

    def _marcar_leido_si_sigue(self, fila: int) -> None:
        actual = self.lista.currentIndex().row()
        if actual == fila:
            self.modelo_lista.marcar([fila], leido=True)
            self._actualizar_contadores()

    # -------------------------------------------------------------- acciones
    def _filas_seleccionadas(self) -> list[int]:
        return sorted({i.row() for i in self.lista.selectionModel().selectedRows()}) or (
            [self.lista.currentIndex().row()] if self.lista.currentIndex().isValid() else []
        )

    def _ids_seleccionados(self) -> list[str]:
        return [
            e.id
            for e in (self.modelo_lista.entrada(f) for f in self._filas_seleccionadas())
            if e is not None
        ]

    def _alternar(self, que: str) -> None:
        filas = self._filas_seleccionadas()
        if not filas:
            return
        entrada = self.modelo_lista.entrada(filas[0])
        leido, guardado = self.modelo_lista.estados.get(entrada.id, (False, False))
        if que == "leido":
            self.modelo_lista.marcar(filas, leido=not leido)
        else:
            self.modelo_lista.marcar(filas, guardado=not guardado)
        self._actualizar_contadores()

    def _siguiente_sin_leer(self) -> None:
        actual = self.lista.currentIndex().row()
        siguiente = self.modelo_lista.siguiente_sin_leer(actual)
        if siguiente < 0:
            self.statusBar().showMessage("No queda nada por leer aquí", 3000)
            return
        self.lista.selectRow(siguiente)

    def _avanzar(self) -> None:
        """Barra espaciadora: baja por el artículo y salta al siguiente al final."""
        if not self.articulo.avanzar_pagina():
            self._siguiente_sin_leer()

    def _marcar_todo_leido(self) -> None:
        ids = [e.id for e in self.modelo_lista.entradas]
        if not ids:
            return
        repo.set_read(self.conn, ids, True)
        self.modelo_lista.recargar()
        self._actualizar_contadores()

    def _abrir_en_navegador(self) -> None:
        entrada = self.articulo.entrada_actual
        if entrada and entrada.url:
            QDesktopServices.openUrl(QUrl(entrada.url))

    def _buscar(self) -> None:
        texto = self.buscador.text().strip()
        if not texto:
            return
        self.modelo_lista.set_seleccion(EntrySelection(query=texto, limit=200))
        self.statusBar().showMessage(
            f"{self.modelo_lista.rowCount()} resultados para «{texto}»", 4000
        )

    def _suscribirse(self) -> None:
        url, ok = QInputDialog.getText(
            self, "Suscribirse", "Dirección del feed o de la web:", QLineEdit.EchoMode.Normal
        )
        if ok and url.strip():
            self._lanzar(self._alta(url.strip()))

    def _importar_opml(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        ruta, _ = QFileDialog.getOpenFileName(self, "Importar OPML", "", "OPML (*.opml *.xml)")
        if not ruta:
            return
        from rsscore.opml import import_opml

        resultado = import_opml(self.conn, Path(ruta).read_bytes())
        self.modelo_arbol.recargar()
        self.arbol.expandAll()
        self.statusBar().showMessage(f"OPML importado: {resultado}", 6000)

    def _exportar_opml(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        from rsscore.opml import export_opml

        ruta, _ = QFileDialog.getSaveFileName(
            self, "Exportar OPML", "suscripciones.opml", "OPML (*.opml)"
        )
        if ruta:
            Path(ruta).write_text(export_opml(self.conn), encoding="utf-8")
            self.statusBar().showMessage(f"Escrito en {ruta}", 5000)

    def _actualizar_contadores(self) -> None:
        sin_leer = self.conn.execute(
            "SELECT COUNT(*) AS n FROM entry_state WHERE read = 0"
        ).fetchone()["n"]
        self.bandeja.actualizar_contador(sin_leer)
        self.etiqueta_estado.setText(f"  {sin_leer} sin leer  ")

    # -------------------------------------------------------- tareas async
    def _lanzar(self, corrutina) -> None:
        """Guarda la referencia: sin ella el recolector puede matar la tarea a medias."""
        tarea = asyncio.ensure_future(corrutina)
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)

    async def _alta(self, url: str) -> None:
        self.statusBar().showMessage(f"Suscribiendo a {url}…")
        from rsscore.ingest import Ingestor, NoFeedFound

        try:
            async with Ingestor(self.conn, self.cfg) as ing:
                feed = await ing.add_by_url(url)
        except NoFeedFound as exc:
            feed = await self._ofrecer_raspado(url, exc)
            if feed is None:
                return
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo suscribir", str(exc))
            return
        self.modelo_arbol.recargar()
        self.arbol.expandAll()
        self._actualizar_contadores()
        self.statusBar().showMessage(f"Suscrito a {feed.display_title}", 5000)

    async def _ofrecer_raspado(self, url: str, fallo) -> object | None:
        """Sin feed, pero quizá se pueda raspar: se enseña qué saldría.

        Dar de alta a ciegas un raspado que no funciona es peor que no ofrecerlo,
        así que primero se muestra la muestra de titulares que se extraerían.
        """
        from rsscore.ingest import Ingestor

        if not fallo.candidates:
            QMessageBox.information(
                self,
                "Sin feed",
                f"{url} no publica ningún feed y no he reconocido un listado de "
                "artículos.\n\nPuedes vigilar los cambios de la página con:\n"
                f"    rss watch {url}",
            )
            return None

        mejor = fallo.candidates[0]
        muestra = "\n".join(f"  · {m[:70]}" for m in mejor.sample)
        respuesta = QMessageBox.question(
            self,
            "Sin feed, pero se puede raspar",
            f"{url} no publica feed.\n\nHe reconocido {mejor.count} artículos con "
            f"«{mejor.config.item_selector}»:\n\n{muestra}\n\n¿Lo doy de alta así?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return None

        async with Ingestor(self.conn, self.cfg) as ing:
            return await ing.add_source(url, "scrape", mejor.config.model_dump())

    async def _refrescar(self, *, todos: bool = False) -> None:
        self.statusBar().showMessage("Actualizando…")
        feeds, nuevas = await self.backend.refrescar(todos=todos)
        self.modelo_arbol.recargar()
        self.arbol.expandAll()
        self.modelo_lista.recargar()
        self._actualizar_contadores()
        self.statusBar().showMessage(f"{feeds} feeds · {nuevas} artículos nuevos", 6000)
        if nuevas:
            self.bandeja.avisar("Lector RSS", f"{nuevas} artículos nuevos")

    async def _sincronizar(self) -> None:
        self.statusBar().showMessage("Sincronizando…")
        resultado = await self.backend.sincronizar()
        self.modelo_arbol.recargar()
        self.modelo_lista.recargar()
        self._actualizar_contadores()
        self.statusBar().showMessage(f"Sincronizado · {resultado}", 6000)

    async def _exportar_obsidian(self) -> None:
        ids = self._ids_seleccionados()
        if not ids:
            return
        try:
            rutas = await self.backend.exportar_obsidian(ids)
        except Exception as exc:
            QMessageBox.warning(self, "Exportación a Obsidian", str(exc))
            return
        self.statusBar().showMessage(f"{len(rutas)} notas escritas en la bóveda", 6000)

    async def _enviar_kindle(self) -> None:
        ids = self._ids_seleccionados()
        if not ids:
            return
        try:
            mensaje = await self.backend.enviar_kindle(ids)
        except Exception as exc:
            QMessageBox.warning(self, "Envío al Kindle", str(exc))
            return
        self.statusBar().showMessage(mensaje, 6000)

    async def _revista(self) -> None:
        seleccion = self.modelo_lista.seleccion.model_copy()
        seleccion.limit = self.cfg.magazine.max_articles
        seleccion.offset = 0
        try:
            ruta = await self.backend.generar_revista(seleccion)
        except Exception as exc:
            QMessageBox.warning(self, "Revista EPUB", str(exc))
            return
        self.statusBar().showMessage(f"Revista generada: {ruta}", 8000)
        self.bandeja.avisar("Revista lista", ruta)

    # ---------------------------------------------------------------- bucles
    def arrancar_tareas(self) -> None:
        self._lanzar(self.backend.bucle_refresco(self.stop, self._al_refrescar_en_segundo_plano))
        self._lanzar(self.backend.bucle_sync(self.stop, lambda r: None))
        self._lanzar(self.backend.worker_exportaciones(self.stop))

    def _al_refrescar_en_segundo_plano(self, feeds: int, nuevas: int) -> None:
        self.modelo_arbol.recargar()
        self.arbol.expandAll()
        self._actualizar_contadores()
        if nuevas:
            self.bandeja.avisar("Lector RSS", f"{nuevas} artículos nuevos")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rssdesk", description="Lector RSS de escritorio")
    parser.add_argument("--config")
    parser.add_argument("--db")
    parser.add_argument(
        "--check", action="store_true", help="construye la ventana y sale (para pruebas)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    cfg = Config.load(args.config)
    if args.db:
        cfg.db_path = Path(args.db)
    conn = open_db(cfg.db_path, device_name=cfg.device_name or "escritorio")

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Lector RSS")
    app.setQuitOnLastWindowClosed(False)  # vive en la bandeja

    bucle = qasync.QEventLoop(app)
    asyncio.set_event_loop(bucle)

    ventana = MainWindow(conn, cfg)
    ventana.show()

    if args.check:
        print(
            f"Ventana construida: {ventana.modelo_arbol.rowCount()} grupos, "
            f"{ventana.modelo_lista.rowCount()} artículos en la lista"
        )
        return 0

    ventana.arrancar_tareas()
    with bucle:
        bucle.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
