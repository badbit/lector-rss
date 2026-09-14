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
from html import escape
from pathlib import Path

import qasync
from PySide6.QtCore import QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
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
from rsscore.config import Config, default_config_path
from rsscore.db import open_db
from rsscore.models import EntrySelection

from .article import ArticleView
from .icons import action_icon, app_icon
from .models import ROL_ID, ROL_TIPO, EntryListModel, FeedTreeModel
from .settings import edit_settings, save_toolbar_style
from .tasks import Backend
from .tray import Tray

log = logging.getLogger("rssdesk")

# Valores de ``desktop.toolbar_style``: texto del menú y aspecto de los botones.
ESTILOS_BARRA = {
    "text": ("Solo &texto", Qt.ToolButtonStyle.ToolButtonTextOnly),
    "text_and_icons": ("Texto &e íconos", Qt.ToolButtonStyle.ToolButtonTextBesideIcon),
    "icons": ("Solo í&conos", Qt.ToolButtonStyle.ToolButtonIconOnly),
}


class MainWindow(QMainWindow):
    def __init__(self, conn, cfg: Config, config_path: Path | None = None) -> None:
        super().__init__()
        self.conn = conn
        self.cfg = cfg
        self.config_path = config_path or default_config_path()
        self.backend = Backend(conn, cfg)
        self.stop = asyncio.Event()
        self._tareas: set[asyncio.Task] = set()
        self.setWindowTitle("Lector RSS")
        self.setWindowIcon(app_icon())
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
        barra = self.barra = QToolBar("Principal")
        barra.setMovable(False)
        self.addToolBar(barra)
        menu_archivo = self.menuBar().addMenu("&Archivo")
        menu_suscripciones = self.menuBar().addMenu("&Suscripciones")
        menu_ver = self.menuBar().addMenu("&Ver")
        menu_exportar = self.menuBar().addMenu("&Exportar")

        def accion(texto, atajo, slot, *, icono=None, ayuda=None, en_barra=False, menu=None):
            a = QAction(texto, self)
            if atajo:
                a.setShortcut(QKeySequence(atajo))
            if icono:
                a.setIcon(action_icon(icono))
            if ayuda:
                # El tooltip nombra el botón aunque la barra sólo muestre íconos;
                # en los menús, la misma ayuda sale en la barra de estado.
                nombre = texto.replace("&", "").removesuffix("…")
                if atajo:
                    tecla = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
                    nombre += f" ({tecla})"
                a.setToolTip(f"<b>{escape(nombre)}</b><br>{escape(ayuda)}")
                a.setStatusTip(ayuda)
            a.triggered.connect(slot)
            self.addAction(a)
            if en_barra:
                barra.addAction(a)
            if menu is not None:
                menu.addAction(a)
            return a

        accion(
            "&Suscribirse…",
            "Ctrl+N",
            self._suscribirse,
            icono="circle-plus",
            ayuda="Añade un feed a partir de su dirección o de la de su web",
            en_barra=True,
            menu=menu_archivo,
        )
        accion(
            "&Actualizar todo",
            "F5",
            lambda: self._lanzar(self._refrescar(todos=True)),
            icono="refresh-cw",
            ayuda="Busca artículos nuevos en todas las suscripciones",
            en_barra=True,
            menu=menu_archivo,
        )
        accion(
            "&Sincronizar",
            "Ctrl+S",
            lambda: self._lanzar(self._sincronizar()),
            icono="cloud-sync",
            ayuda="Intercambia con el hub suscripciones, lecturas y guardados",
            en_barra=True,
            menu=menu_archivo,
        )
        menu_archivo.addSeparator()
        accion(
            "Importar OPML…",
            None,
            self._importar_opml,
            icono="file-input",
            ayuda="Añade las suscripciones de un archivo OPML",
            menu=menu_archivo,
        )
        accion(
            "Exportar OPML…",
            None,
            self._exportar_opml,
            icono="file-output",
            ayuda="Guarda todas las suscripciones en un archivo OPML",
            menu=menu_archivo,
        )
        accion(
            "&Preferencias…",
            "Ctrl+,",
            self._preferencias,
            icono="settings",
            ayuda="Nombre del dispositivo, conexión con el hub y modo de descarga",
            menu=menu_archivo,
        )
        menu_archivo.addSeparator()
        accion(
            "&Salir",
            "Ctrl+Q",
            self._salir,
            icono="log-out",
            ayuda="Cierra el lector; cerrar la ventana solo la esconde en la bandeja",
            menu=menu_archivo,
        )

        # Atajos de lectura, calcados de Liferea.
        accion(
            "Siguiente sin leer",
            "n",
            self._siguiente_sin_leer,
            icono="skip-forward",
            ayuda="Salta al siguiente artículo sin leer de la lista",
            menu=menu_ver,
        )
        accion("Siguiente sin leer (j)", "j", self._siguiente_sin_leer)
        accion(
            "Avanzar",
            "Space",
            self._avanzar,
            icono="chevrons-down",
            ayuda="Baja por el artículo y, al llegar al final, pasa al siguiente sin leer",
            menu=menu_ver,
        )
        accion(
            "Alternar leído",
            "r",
            lambda: self._alternar("leido"),
            icono="mail-open",
            ayuda="Marca los artículos seleccionados como leídos o sin leer",
            menu=menu_ver,
        )
        accion(
            "Alternar guardado",
            "s",
            lambda: self._alternar("guardado"),
            icono="star",
            ayuda="Guarda los artículos seleccionados o los quita de guardados",
            menu=menu_ver,
        )
        accion(
            "Marcar todo como leído",
            "Ctrl+A",
            self._marcar_todo_leido,
            icono="check-check",
            ayuda="Marca como leídos todos los artículos de la lista",
            menu=menu_ver,
        )
        accion(
            "Abrir en el navegador",
            "Ctrl+O",
            self._abrir_en_navegador,
            icono="external-link",
            ayuda="Abre el artículo actual en el navegador web",
            menu=menu_ver,
        )
        accion(
            "Buscar",
            "Ctrl+F",
            lambda: self.buscador.setFocus(),
            icono="search",
            ayuda="Busca en todo el archivo de artículos",
            menu=menu_ver,
        )
        menu_ver.addSeparator()
        self._construir_menu_barra(menu_ver)

        accion(
            "Nueva &carpeta…",
            None,
            self._nueva_carpeta,
            icono="folder-plus",
            ayuda="Crea una carpeta para agrupar suscripciones",
            menu=menu_suscripciones,
        )
        accion(
            "&Renombrar…",
            None,
            self._renombrar_seleccion,
            icono="pencil",
            ayuda="Cambia el nombre de la carpeta o suscripción seleccionada",
            menu=menu_suscripciones,
        )
        accion(
            "&Mover feed…",
            None,
            self._mover_feed,
            icono="folder-input",
            ayuda="Lleva la suscripción seleccionada a otra carpeta",
            menu=menu_suscripciones,
        )
        accion(
            "&Eliminar…",
            "Delete",
            self._eliminar_seleccion,
            icono="trash-2",
            ayuda="Elimina la carpeta o suscripción seleccionada",
            menu=menu_suscripciones,
        )

        accion(
            "A &Obsidian",
            "Ctrl+E",
            lambda: self._lanzar(self._exportar_obsidian()),
            icono="notebook-pen",
            ayuda="Guarda los artículos seleccionados como notas en la bóveda de Obsidian",
            en_barra=True,
            menu=menu_exportar,
        )
        accion(
            "Al &Kindle",
            "Ctrl+K",
            lambda: self._lanzar(self._enviar_kindle()),
            icono="tablet",
            ayuda="Envía por correo los artículos seleccionados al Kindle",
            en_barra=True,
            menu=menu_exportar,
        )
        accion(
            "Generar &revista EPUB",
            "Ctrl+M",
            lambda: self._lanzar(self._revista()),
            icono="newspaper",
            ayuda="Reúne los artículos de la vista actual en una revista EPUB",
            en_barra=True,
            menu=menu_exportar,
        )

        self.etiqueta_estado = QLabel("")
        barra.addSeparator()
        barra.addWidget(self.etiqueta_estado)

    def _construir_menu_barra(self, menu_ver: QMenu) -> None:
        """Ver → Barra de herramientas: solo texto, texto e íconos o solo íconos."""
        submenu = menu_ver.addMenu(action_icon("panel-top"), "&Barra de herramientas")
        self.estilos_barra = QActionGroup(self)
        for clave, (texto, _estilo) in ESTILOS_BARRA.items():
            opcion = self.estilos_barra.addAction(texto)
            opcion.setCheckable(True)
            opcion.setData(clave)
            submenu.addAction(opcion)
        self.estilos_barra.triggered.connect(self._elegir_estilo_barra)
        self._aplicar_estilo_barra(self.cfg.desktop.toolbar_style)

    def _aplicar_estilo_barra(self, estilo: str) -> None:
        self.barra.setToolButtonStyle(ESTILOS_BARRA[estilo][1])
        for opcion in self.estilos_barra.actions():
            opcion.setChecked(opcion.data() == estilo)

    def _elegir_estilo_barra(self, opcion: QAction) -> None:
        estilo = opcion.data()
        self._aplicar_estilo_barra(estilo)
        self.cfg.desktop.toolbar_style = estilo
        try:
            save_toolbar_style(estilo, self.config_path)
        except OSError as exc:
            self.statusBar().showMessage(f"No se pudo guardar el aspecto de la barra: {exc}", 6000)

    def createPopupMenu(self) -> QMenu:
        """Clic derecho sobre la barra: las mismas opciones de aspecto que en Ver."""
        menu = super().createPopupMenu()
        menu.addSeparator()
        menu.addActions(self.estilos_barra.actions())
        return menu

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
            case "inteligente":
                from rsscore.rules.smart import list_saved_searches, saved_search_to_selection

                vista = next((v for v in list_saved_searches(self.conn) if v.id == ident), None)
                if vista is None:
                    return
                seleccion = saved_search_to_selection(self.conn, vista)
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
        if self.cfg.hub_url and not self.conn.execute(
            "SELECT 1 FROM entry_bodies WHERE entry_id = ?", (completa.id,)
        ).fetchone():
            self._lanzar(self._cargar_cuerpo(completa.id))
        # Marcar como leído tras un momento, no al pasar de largo con las flechas.
        QTimer.singleShot(700, lambda fila=index.row(): self._marcar_leido_si_sigue(fila))

    async def _cargar_cuerpo(self, entry_id: str) -> None:
        try:
            completa = await self.backend.cargar_articulo(entry_id)
        except Exception as exc:
            log.warning("No se pudo traer el cuerpo de %s: %s", entry_id, exc)
            return
        actual = self.articulo.entrada_actual
        if completa is None or actual is None or actual.id != entry_id:
            return
        feed = repo.get_feed(self.conn, completa.feed_id)
        etiquetas = [t.name for t in repo.entry_tags(self.conn, completa.id)]
        self.articulo.mostrar(completa, feed.display_title if feed else "", etiquetas)

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

    def _preferencias(self) -> None:
        if not edit_settings(self, self.cfg, self.config_path):
            return
        self.statusBar().showMessage(f"Preferencias guardadas en {self.config_path}", 6000)
        self._lanzar(self._sincronizar())

    def _nodo_seleccionado(self) -> tuple[str | None, str | None]:
        index = self.arbol.currentIndex()
        if not index.isValid():
            return None, None
        return self.modelo_arbol.data(index, ROL_TIPO), self.modelo_arbol.data(index, ROL_ID)

    def _nueva_carpeta(self) -> None:
        from rsscore.models import Folder

        nombre, ok = QInputDialog.getText(self, "Nueva carpeta", "Nombre:")
        if not ok or not nombre.strip():
            return
        repo.upsert_folder(self.conn, Folder(name=nombre.strip()))
        self._recargar_arbol()
        self._lanzar(self._sincronizar())

    def _renombrar_seleccion(self) -> None:
        tipo, ident = self._nodo_seleccionado()
        if tipo == "feed" and ident:
            feed = repo.get_feed(self.conn, ident)
            if not feed:
                return
            nombre, ok = QInputDialog.getText(
                self, "Renombrar suscripción", "Título:", text=feed.display_title
            )
            if ok:
                repo.set_feed_title(self.conn, ident, nombre)
        elif tipo == "carpeta" and ident:
            folder = repo.get_folder(self.conn, ident)
            if not folder:
                return
            nombre, ok = QInputDialog.getText(
                self, "Renombrar carpeta", "Nombre:", text=folder.name
            )
            if ok and nombre.strip():
                repo.rename_folder(self.conn, ident, nombre)
        else:
            self.statusBar().showMessage("Selecciona una carpeta o una suscripción", 4000)
            return
        self._recargar_arbol()
        self._lanzar(self._sincronizar())

    def _mover_feed(self) -> None:
        tipo, ident = self._nodo_seleccionado()
        if tipo != "feed" or not ident:
            self.statusBar().showMessage("Selecciona una suscripción", 4000)
            return
        folders = repo.list_folders(self.conn)
        labels = ["Sin carpeta", *(f.name for f in folders)]
        elegido, ok = QInputDialog.getItem(
            self, "Mover suscripción", "Carpeta:", labels, editable=False
        )
        if not ok:
            return
        folder_id = None if elegido == "Sin carpeta" else folders[labels.index(elegido) - 1].id
        repo.set_feed_folder(self.conn, ident, folder_id)
        self._recargar_arbol()
        self._lanzar(self._sincronizar())

    def _eliminar_seleccion(self) -> None:
        tipo, ident = self._nodo_seleccionado()
        if tipo not in {"feed", "carpeta"} or not ident:
            self.statusBar().showMessage("Selecciona una carpeta o una suscripción", 4000)
            return
        nombre = self.modelo_arbol.data(self.arbol.currentIndex())
        respuesta = QMessageBox.question(
            self,
            "Eliminar",
            f"¿Eliminar «{nombre}»? Los artículos archivados no se borrarán manualmente.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return
        if tipo == "feed":
            repo.delete_feed(self.conn, ident)
        else:
            repo.delete_folder(self.conn, ident)
        self._recargar_arbol()
        self._lanzar(self._sincronizar())

    def _recargar_arbol(self) -> None:
        self.modelo_arbol.recargar()
        self.arbol.expandAll()
        self.modelo_lista.recargar()
        self._actualizar_contadores()

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
            if not self.cfg.desktop_fetches_locally:
                title = await self.backend.suscribirse(url)
                self._recargar_arbol()
                self.statusBar().showMessage(f"Suscrito a {title}", 5000)
                return
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
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    cfg = Config.load(config_path)
    if args.db:
        cfg.db_path = Path(args.db)
    conn = open_db(cfg.db_path, device_name=cfg.device_name or "escritorio")

    app = QApplication(["rssdesk"])
    app.setApplicationName("Lector RSS")
    app.setDesktopFileName("org.badbit.LectorRSS")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)  # vive en la bandeja

    bucle = qasync.QEventLoop(app)
    asyncio.set_event_loop(bucle)

    ventana = MainWindow(conn, cfg, config_path)
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
