"""Texto completo: rescatar el artículo cuando el feed solo manda el aperitivo.

Muchos medios publican feeds truncados a dos frases para forzar la visita. Aquí
se descarga la página del artículo y se le pide a trafilatura el contenido
principal, descartando menús, comentarios y banners.

Dos decisiones que no son evidentes:

* La extracción se hace **solo cuando hace falta** (`should_extract`). Bajar la
  página de cada entrada de 500 feeds sería multiplicar por veinte el tráfico y
  hacer que el hub parezca un rastreador agresivo.
* `trafilatura.extract` es sincrónico y consume CPU (parsea el DOM y puntúa los
  bloques), así que se ejecuta en un hilo aparte para no bloquear el bucle de
  eventos mientras el resto de descargas están en vuelo.
"""

from __future__ import annotations

import logging

import anyio
import httpx
import trafilatura

from .config import FetchConfig
from .fetch import fetch_url
from .models import Entry, Feed
from .parse import html_to_text, sanitize_html

log = logging.getLogger(__name__)

# Por debajo de esto damos por hecho que el feed viene recortado.
SHORT_BODY_CHARS = 800

# Colas típicas de un resumen truncado.
ELLIPSIS_MARKERS = ("…", "...", "[…]", "[...]", "(…)", "(...)")
PHRASE_MARKERS = (
    "read more", "read the rest", "continue reading", "keep reading",
    "seguir leyendo", "leer más", "leer mas", "ver más", "ver mas", "continuar leyendo",
    "artículo completo", "articulo completo", "lire la suite", "weiterlesen", "leia mais",
)


def should_extract(entry: Entry, feed: Feed, cfg: FetchConfig) -> bool:
    """¿Merece la pena bajar el artículo entero?

    Sí si el feed lo pide explícitamente, y también si el cuerpo huele a
    truncado: muy corto o terminado en una invitación a seguir leyendo.
    """
    if not entry.url:
        return False
    if feed.fetch_full_text or cfg.full_text_default:
        return True

    text = (entry.body_text or "").strip()
    if not text:
        return True
    if len(text) < SHORT_BODY_CHARS:
        return True
    return is_truncated(text)


def is_truncated(text: str) -> bool:
    """Detecta la cola de un resumen recortado en las últimas ~80 letras."""
    tail = text[-80:].strip().casefold()
    if tail.endswith(ELLIPSIS_MARKERS):
        return True
    # Quitamos la puntuación y los adornos de un «Seguir leyendo →» antes de comparar.
    tail = tail.rstrip(" .:;·|>»→-]›")
    return tail.endswith(tuple(m.rstrip(" .…") for m in PHRASE_MARKERS))


async def extract_full_text(
    client: httpx.AsyncClient, url: str, cfg: FetchConfig
) -> tuple[str | None, str | None]:
    """Devuelve `(html, texto)` del artículo, o `(None, None)` si no se pudo."""
    if not url:
        return None, None

    result = await fetch_url(client, url, cfg, headers={"Accept": "text/html,*/*;q=0.8"})
    if not result.ok or not result.content:
        log.info("no se pudo bajar el texto completo de %s: %s", url, result.error)
        return None, None

    page = result.text()
    if not page.strip():
        return None, None
    return await extract_from_html(page, result.final_url or url)


async def extract_from_html(html: str, url: str = "") -> tuple[str | None, str | None]:
    """Extrae el contenido principal de un HTML ya descargado, fuera del bucle."""
    try:
        return await anyio.to_thread.run_sync(_extract_sync, html, url)
    except Exception as exc:                       # trafilatura puede morir con HTML raro
        log.info("extracción fallida en %s: %s", url, exc)
        return None, None


def _extract_sync(html: str, url: str) -> tuple[str | None, str | None]:
    common = {
        "url": url or None,
        "include_comments": False,
        "include_tables": True,
        "include_images": True,
        "favor_precision": True,
    }
    try:
        raw_html = trafilatura.extract(html, output_format="html", **common)
        text = trafilatura.extract(html, output_format="txt", **common)
    except Exception as exc:
        log.info("trafilatura falló en %s: %s", url, exc)
        return None, None

    clean_html = sanitize_html(raw_html, base_url=url) if raw_html else None
    clean_text = (text or "").strip() or (html_to_text(clean_html) if clean_html else "")
    if not clean_html and not clean_text:
        return None, None
    return clean_html, clean_text or None
