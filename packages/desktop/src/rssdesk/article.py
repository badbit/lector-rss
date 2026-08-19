"""Visor de artículos.

Se usa `QTextBrowser` y no un motor web completo a propósito: no ejecuta
JavaScript ni pide recursos remotos, así que abrir un artículo no avisa a nadie
de que lo has leído. El HTML ya viene saneado del núcleo.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QTextBrowser
from rsscore.models import Entry

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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._al_pulsar_enlace)
        self.entrada_actual: Entry | None = None
        self.limpiar()

    def _al_pulsar_enlace(self, url: QUrl) -> None:
        """Los enlaces se abren en el navegador del sistema, no aquí dentro."""
        QDesktopServices.openUrl(url)

    def limpiar(self) -> None:
        self.entrada_actual = None
        self.setHtml(
            HOJA + "<body><p style='color:#888;font-family:sans-serif'>"
            "Selecciona un artículo.</p></body>"
        )

    def mostrar(
        self, entrada: Entry, feed_titulo: str = "", etiquetas: list[str] | None = None
    ) -> None:
        self.entrada_actual = entrada
        fecha = datetime.fromtimestamp(entrada.published_at / 1000).strftime("%d/%m/%Y %H:%M")
        partes = [p for p in (feed_titulo, entrada.author, fecha) if p]
        meta = " · ".join(partes)
        if etiquetas:
            meta += " · " + " ".join(f"#{t}" for t in etiquetas)

        cuerpo = entrada.body_html or ""
        if not cuerpo and entrada.body_text:
            cuerpo = "<p>" + entrada.body_text.replace("\n\n", "</p><p>") + "</p>"
        if not cuerpo:
            cuerpo = f"<p><i>{entrada.summary or 'Sin contenido.'}</i></p>"

        enlace = f"<p><a href='{entrada.url}'>Abrir el original ↗</a></p>" if entrada.url else ""
        self.setHtml(
            f"{HOJA}<body><h1>{_escapar(entrada.title)}</h1>"
            f"<div class='meta'>{_escapar(meta)}</div>{cuerpo}{enlace}</body>"
        )
        self.verticalScrollBar().setValue(0)

    def avanzar_pagina(self) -> bool:
        """Avanza una pantalla. Devuelve False si ya estaba al final."""
        barra = self.verticalScrollBar()
        if barra.value() >= barra.maximum():
            return False
        barra.setValue(min(barra.value() + barra.pageStep(), barra.maximum()))
        return True


def _escapar(texto: str | None) -> str:
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
