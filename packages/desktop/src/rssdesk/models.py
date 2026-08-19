"""Modelos Qt sobre el núcleo.

La lista de artículos usa `canFetchMore`/`fetchMore` en vez de cargarlo todo:
con archivo permanente una carpeta puede tener cientos de miles de entradas y
construir esa lista entera congelaría la interfaz.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QAbstractItemModel, QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QFont
from rsscore import repo
from rsscore.models import Entry, EntrySelection

PAGINA = 200

ROL_ID = Qt.ItemDataRole.UserRole + 1
ROL_TIPO = Qt.ItemDataRole.UserRole + 2
ROL_SIN_LEER = Qt.ItemDataRole.UserRole + 3


# ============================================================ árbol de feeds
@dataclass
class NodoArbol:
    tipo: str  # 'especial' | 'carpeta' | 'feed' | 'inteligente'
    id: str
    nombre: str
    padre: NodoArbol | None = None
    hijos: list[NodoArbol] = field(default_factory=list)
    sin_leer: int = 0
    error: str | None = None

    def fila(self) -> int:
        return self.padre.hijos.index(self) if self.padre else 0


class FeedTreeModel(QAbstractItemModel):
    """Carpetas, feeds y vistas especiales, con contadores de no leídos."""

    def __init__(self, conn: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.raiz = NodoArbol("raiz", "", "")
        self.recargar()

    # ---------------------------------------------------------------- datos
    def recargar(self) -> None:
        self.beginResetModel()
        self.raiz = NodoArbol("raiz", "", "")
        contadores = repo.unread_counts(self.conn)

        especiales = NodoArbol("grupo", "__especiales", "Vistas", self.raiz)
        self.raiz.hijos.append(especiales)
        total = sum(contadores.values())
        for ident, nombre, n in (
            ("todos", "Todos los artículos", total),
            ("sin_leer", "Sin leer", total),
            ("guardados", "Guardados", self._guardados()),
        ):
            especiales.hijos.append(NodoArbol("especial", ident, nombre, especiales, sin_leer=n))

        carpetas = {f.id: f for f in repo.list_folders(self.conn)}
        nodos: dict[str, NodoArbol] = {}
        for carpeta in carpetas.values():
            nodos[carpeta.id] = NodoArbol("carpeta", carpeta.id, carpeta.name)
        for carpeta in carpetas.values():
            nodo = nodos[carpeta.id]
            padre = nodos.get(carpeta.parent_id) if carpeta.parent_id else self.raiz
            padre = padre or self.raiz
            nodo.padre = padre
            padre.hijos.append(nodo)

        for feed in repo.list_feeds(self.conn):
            padre = nodos.get(feed.folder_id) if feed.folder_id else self.raiz
            padre = padre or self.raiz
            nodo = NodoArbol(
                "feed",
                feed.id,
                feed.display_title,
                padre,
                sin_leer=contadores.get(feed.id, 0),
                error=feed.last_error,
            )
            padre.hijos.append(nodo)

        self._propagar(self.raiz)
        self.endResetModel()

    def _guardados(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM entry_state WHERE starred = 1"
        ).fetchone()["n"]

    def _propagar(self, nodo: NodoArbol) -> int:
        """Una carpeta muestra la suma de los no leídos de lo que contiene."""
        if nodo.tipo in ("feed", "especial"):
            return nodo.sin_leer
        total = sum(self._propagar(h) for h in nodo.hijos)
        if nodo.tipo == "carpeta":
            nodo.sin_leer = total
        return total if nodo.tipo != "grupo" else 0

    # ------------------------------------------------------- API de QAbstractItemModel
    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        padre = parent.internalPointer() if parent.isValid() else self.raiz
        if row < 0 or row >= len(padre.hijos):
            return QModelIndex()
        return self.createIndex(row, column, padre.hijos[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        nodo: NodoArbol = index.internalPointer()
        if nodo.padre is None or nodo.padre is self.raiz:
            return QModelIndex()
        return self.createIndex(nodo.padre.fila(), 0, nodo.padre)

    def rowCount(self, parent=QModelIndex()) -> int:
        nodo = parent.internalPointer() if parent.isValid() else self.raiz
        return len(nodo.hijos)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        nodo: NodoArbol = index.internalPointer()
        match role:
            case Qt.ItemDataRole.DisplayRole:
                if nodo.sin_leer and nodo.tipo != "grupo":
                    return f"{nodo.nombre}  ({nodo.sin_leer})"
                return nodo.nombre
            case Qt.ItemDataRole.FontRole:
                fuente = QFont()
                fuente.setBold(bool(nodo.sin_leer) and nodo.tipo != "grupo")
                return fuente
            case Qt.ItemDataRole.ToolTipRole:
                return nodo.error or nodo.nombre
            case _ if role == ROL_ID:
                return nodo.id
            case _ if role == ROL_TIPO:
                return nodo.tipo
            case _ if role == ROL_SIN_LEER:
                return nodo.sin_leer
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return "Suscripciones"
        return None


# =========================================================== lista de artículos
class EntryListModel(QAbstractTableModel):
    COLUMNAS = ("", "Título", "Feed", "Fecha")

    def __init__(self, conn: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.seleccion = EntrySelection(limit=PAGINA)
        self.entradas: list[Entry] = []
        self.estados: dict[str, tuple[bool, bool]] = {}
        self.titulos_feed: dict[str, str] = {}
        self._agotado = False
        self.recargar()

    # ------------------------------------------------------------- consulta
    def set_seleccion(self, seleccion: EntrySelection) -> None:
        self.seleccion = seleccion
        self.seleccion.offset = 0
        self.seleccion.limit = PAGINA
        self.recargar()

    def recargar(self) -> None:
        self.beginResetModel()
        self.titulos_feed = {f.id: f.display_title for f in repo.list_feeds(self.conn)}
        self.seleccion.offset = 0
        self.entradas = repo.select_entries(self.conn, self.seleccion)
        self._agotado = len(self.entradas) < self.seleccion.limit
        self._cargar_estados(self.entradas)
        self.endResetModel()

    def _cargar_estados(self, entradas: list[Entry]) -> None:
        if not entradas:
            return
        marcas = ",".join("?" * len(entradas))
        filas = self.conn.execute(
            f"SELECT entry_id, read, starred FROM entry_state WHERE entry_id IN ({marcas})",
            [e.id for e in entradas],
        ).fetchall()
        for f in filas:
            self.estados[f["entry_id"]] = (bool(f["read"]), bool(f["starred"]))

    # ---------------------------------------------------------- paginación
    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and not self._agotado

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or self._agotado:
            return
        self.seleccion.offset = len(self.entradas)
        nuevas = repo.select_entries(self.conn, self.seleccion)
        if not nuevas:
            self._agotado = True
            return
        inicio = len(self.entradas)
        self.beginInsertRows(QModelIndex(), inicio, inicio + len(nuevas) - 1)
        self.entradas.extend(nuevas)
        self._cargar_estados(nuevas)
        self.endInsertRows()
        if len(nuevas) < self.seleccion.limit:
            self._agotado = True

    # ------------------------------------------------------------- interfaz
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entradas)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNAS)

    def entrada(self, fila: int) -> Entry | None:
        return self.entradas[fila] if 0 <= fila < len(self.entradas) else None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entrada = self.entradas[index.row()]
        leido, guardado = self.estados.get(entrada.id, (False, False))
        match role:
            case Qt.ItemDataRole.DisplayRole:
                match index.column():
                    case 0:
                        return "★" if guardado else ""
                    case 1:
                        return entrada.title or "(sin título)"
                    case 2:
                        return self.titulos_feed.get(entrada.feed_id, "")
                    case 3:
                        return datetime.fromtimestamp(entrada.published_at / 1000).strftime(
                            "%d/%m %H:%M"
                        )
            case Qt.ItemDataRole.FontRole:
                fuente = QFont()
                fuente.setBold(not leido)
                return fuente
            case _ if role == ROL_ID:
                return entrada.id
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNAS[section]
        return None

    # ------------------------------------------------------------- acciones
    def marcar(
        self, filas: list[int], *, leido: bool | None = None, guardado: bool | None = None
    ) -> None:
        ids = [self.entradas[f].id for f in filas if 0 <= f < len(self.entradas)]
        if not ids:
            return
        if leido is not None:
            repo.set_read(self.conn, ids, leido)
        if guardado is not None:
            repo.set_starred(self.conn, ids, guardado)
        for entry_id in ids:
            antes = self.estados.get(entry_id, (False, False))
            self.estados[entry_id] = (
                antes[0] if leido is None else leido,
                antes[1] if guardado is None else guardado,
            )
        if filas:
            self.dataChanged.emit(
                self.index(min(filas), 0), self.index(max(filas), self.columnCount() - 1)
            )

    def siguiente_sin_leer(self, desde: int) -> int:
        for fila in range(desde + 1, len(self.entradas)):
            if not self.estados.get(self.entradas[fila].id, (False, False))[0]:
                return fila
        return -1
