"""Visor de artículos.

Se usa `QTextBrowser`: no ejecuta JavaScript. Las imágenes HTTP se descargan
asíncronamente sin cookies; esas peticiones sí son visibles para sus servidores.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPalette, QTextCursor, QTextDocument
from PySide6.QtWidgets import QTextBrowser
from rsscore.models import Entry
from rsscore.parse import sanitize_html

from .resources import RemoteResources, decode_image, web_url

HOJA = """
<style>
  body   { font-family: Georgia, 'DejaVu Serif', serif; font-size: 15px; line-height: 1.6; }
  h1     { font-size: 20px; margin: 0 0 4px 0; font-family: sans-serif; }
  .meta  { color: #777; font-size: 12px; font-family: sans-serif;
           margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #ccc; }
  a      { color: #2a6496; }
  pre, code { font-family: 'DejaVu Sans Mono', monospace; font-size: 13px;
              background: rgba(128,128,128,0.10); }
  pre    { padding: 8px; }
  blockquote { border-left: 3px solid #bbb; margin-left: 0; padding-left: 12px; color: #555; }
  img    { max-width: 100%; }
</style>
"""


class ArticleView(QTextBrowser):
    solicitar_apertura = Signal(str)

    def __init__(self, parent=None, resources=None) -> None:
        super().__init__(parent)
        self.resources = resources or RemoteResources(self)
        self._generation = 0
        self._images: dict[str, QImage] = {}
        self._requested: set[str] = set()
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._resize_images)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._al_pulsar_enlace)
        self.entrada_actual: Entry | None = None
        self.limpiar()

    def _al_pulsar_enlace(self, url: QUrl) -> None:
        """Los enlaces se abren en el navegador del sistema, no aquí dentro."""
        if web_url(url.toString()):
            QDesktopServices.openUrl(url)

    def limpiar(self) -> None:
        self._reset_images()
        self.entrada_actual = None
        self.setHtml(
            HOJA + "<body><p style='color:#888;font-family:sans-serif'>"
            "Selecciona un artículo.</p></body>"
        )

    def mostrar(
        self, entrada: Entry, feed_titulo: str = "", etiquetas: list[str] | None = None
    ) -> None:
        self._reset_images()
        self.entrada_actual = entrada
        fecha = datetime.fromtimestamp(entrada.published_at / 1000).strftime("%d/%m/%Y %H:%M")
        partes = [p for p in (feed_titulo, entrada.author, fecha) if p]
        meta = " · ".join(partes)
        if etiquetas:
            meta += " · " + " ".join(f"#{t}" for t in etiquetas)

        cuerpo = entrada.body_html or ""
        if not cuerpo and entrada.body_text:
            cuerpo = "<p>" + escape(entrada.body_text).replace("\n\n", "</p><p>") + "</p>"
        if not cuerpo:
            cuerpo = f"<p><i>{escape(entrada.summary or 'Sin contenido.')}</i></p>"

        cuerpo = sanitize_html(cuerpo, base_url=entrada.url or "")
        titulo = escape(entrada.title or "(sin título)")
        enlace = ""
        if entrada.url and web_url(entrada.url):
            href = escape(entrada.url, quote=True)
            titulo = f'<a href="{href}">{titulo}</a>'
            enlace = f'<p><a href="{href}">Abrir el original ↗</a></p>'
        self.document().setBaseUrl(QUrl(entrada.url or ""))
        hoja = HOJA
        if self.palette().color(QPalette.ColorRole.Base).lightness() < 128:
            hoja = hoja.replace("#2a6496", "#79b8ff")
        self.setHtml(
            f"{hoja}<body><h1>{titulo}</h1>"
            f"<div class='meta'>{_escapar(meta)}</div>{cuerpo}{enlace}</body>"
        )
        self.verticalScrollBar().setValue(0)

    def _reset_images(self):
        self._generation += 1
        self._images.clear()
        self._requested.clear()

    def loadResource(self, resource_type, url):
        # No delegar en QTextBrowser: permitiría leer file:// del HTML remoto.
        if resource_type != QTextDocument.ResourceType.ImageResource:
            return None
        name = self.document().baseUrl().resolved(url).toString()
        if name in self._images:
            return self._images[name]
        if not web_url(name) or name in self._requested or len(self._requested) >= 80:
            return None
        self._requested.add(name)
        generation = self._generation

        def loaded(data):
            if generation != self._generation:
                return
            image = decode_image(data)
            if image.isNull() or sum(i.sizeInBytes() for i in self._images.values()) + (
                image.sizeInBytes()
            ) > 48 * 1024 * 1024:
                return
            self._images[name] = image
            self.document().addResource(QTextDocument.ResourceType.ImageResource,
                                        QUrl(name), image)
            self._resize_timer.start(0)

        self.resources.fetch(name, loaded, priority=True)
        return self._images.get(name)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_resize_timer"):
            self._resize_timer.start(0)

    def _resize_images(self):
        document = self.document()
        block = document.begin()
        width = max(40, self.viewport().width() - 32)
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                fmt = fragment.charFormat()
                if fmt.isImageFormat():
                    fmt = fmt.toImageFormat()
                    name = document.baseUrl().resolved(QUrl(fmt.name())).toString()
                    image = self._images.get(name)
                    if image is not None:
                        w = min(width, image.width())
                        fmt.setWidth(w)
                        fmt.setHeight(w * image.height() / image.width())
                        cursor = QTextCursor(document)
                        cursor.setPosition(fragment.position())
                        cursor.setPosition(fragment.position() + fragment.length(),
                                           QTextCursor.MoveMode.KeepAnchor)
                        cursor.setCharFormat(fmt)
                iterator += 1
            block = block.next()
        document.markContentsDirty(0, document.characterCount())
        self.viewport().update()

    def avanzar_pagina(self) -> bool:
        """Avanza una pantalla. Devuelve False si ya estaba al final."""
        barra = self.verticalScrollBar()
        if barra.value() >= barra.maximum():
            return False
        barra.setValue(min(barra.value() + barra.pageStep(), barra.maximum()))
        return True


def _escapar(texto: str | None) -> str:
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
