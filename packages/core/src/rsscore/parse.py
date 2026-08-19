"""De XML a entidades: feedparser + normalización defensiva.

Los feeds del mundo real son un desastre: fechas en formatos inventados, GUID que
cambian en cada publicación, enlaces relativos, HTML con restos de plantilla y
balizas de rastreo. Aquí está el trabajo sucio que hace que el resto del sistema
pueda dar por buenos sus datos:

* **GUID estable**: `id` → `link` → huella del contenido. Nunca la fecha de
  descarga, porque eso duplicaría la entrada en cada refresco.
* **Fechas creíbles**: una fecha rota o del año 2087 se sustituye por el instante
  de la descarga; si no, un feed mal configurado se queda clavado arriba en la
  lista para siempre.
* **HTML saneado**: fuera `<script>`, `<style>`, `<iframe>`, atributos `on*` y
  píxeles de rastreo. El cuerpo se guarda ya limpio, no al mostrarlo, para que
  ningún consumidor (escritorio, móvil, EPUB, Obsidian) tenga que repetirlo.

Una regla por encima de todas: esto **no lanza excepciones**. Un feed malformado
produce lo que se pueda rescatar y un aviso en el log.
"""

from __future__ import annotations

import calendar
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup, Comment, Tag
from dateutil import parser as date_parser

from .ids import hash_content, hash_guid, now_ms
from .models import Entry

log = logging.getLogger(__name__)

SUMMARY_MAX = 500
FUTURE_TOLERANCE_MS = 24 * 3600 * 1000

# Etiquetas que jamás deben sobrevivir en un cuerpo almacenado.
BLOCKED_TAGS = frozenset(
    {"script", "style", "iframe", "object", "embed", "form", "input", "button",
     "noscript", "link", "meta", "base", "applet", "frame", "frameset"}
)
# Atributos peligrosos más allá de los `on*`.
BLOCKED_ATTRS = frozenset({"srcdoc", "formaction", "background", "ping"})

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_ONE_PX = re.compile(r"(width|height)\s*:\s*[01](\.\d+)?\s*(px)?", re.IGNORECASE)


@dataclass(slots=True)
class ParsedFeed:
    """Un feed ya normalizado: metadatos del canal y entradas listas para insertar."""

    title: str = ""
    site_url: str | None = None
    description: str | None = None
    icon_url: str | None = None
    entries: list[Entry] = field(default_factory=list)
    bozo: bool = False
    bozo_error: str | None = None
    version: str = ""

    @property
    def is_feed(self) -> bool:
        """`version` vacía = feedparser no reconoció ningún formato de sindicación."""
        return bool(self.version) or bool(self.entries)


# ==================================================================== fachada
def parse_feed(content: bytes | str, feed_id: str, *, base_url: str = "") -> ParsedFeed:
    """Convierte el XML descargado en un `ParsedFeed`. Nunca lanza."""
    fetched_at = now_ms()
    try:
        doc = feedparser.parse(content)
    except Exception as exc:                       # feedparser es tolerante, pero por si acaso
        log.warning("feedparser falló por completo en %s: %s", feed_id, exc)
        return ParsedFeed(bozo=True, bozo_error=str(exc))

    bozo = bool(doc.get("bozo"))
    bozo_error = str(doc.get("bozo_exception")) if bozo and doc.get("bozo_exception") else None
    if bozo:
        # bozo no implica inservible: casi siempre queda contenido aprovechable.
        log.info("feed malformado (%s): %s", feed_id, bozo_error)

    channel = doc.get("feed") or {}
    site_url = _absolute(_first_link(channel), base_url)
    parsed = ParsedFeed(
        title=_plain(channel.get("title") or ""),
        site_url=site_url,
        description=_plain(channel.get("subtitle") or channel.get("description") or "") or None,
        icon_url=_channel_icon(channel, base_url or site_url or ""),
        bozo=bozo,
        bozo_error=bozo_error,
        version=doc.get("version") or "",
    )

    # Los enlaces relativos de las entradas se resuelven contra el sitio si lo hay.
    entry_base = site_url or base_url
    seen: set[str] = set()
    for raw in doc.get("entries") or []:
        try:
            entry = _build_entry(raw, feed_id, entry_base, fetched_at)
        except Exception as exc:                   # una entrada rota no tumba el feed
            log.warning("entrada ilegible en %s: %s", feed_id, exc)
            continue
        if entry.guid_hash in seen:
            # Feeds que repiten el mismo guid en el mismo documento: nos quedamos
            # con la primera, porque `entries` tiene UNIQUE(feed_id, guid_hash).
            log.info("guid duplicado dentro del mismo documento en %s", feed_id)
            continue
        seen.add(entry.guid_hash)
        parsed.entries.append(entry)
    return parsed


# =================================================================== entradas
def _build_entry(raw: Any, feed_id: str, base_url: str, fetched_at: int) -> Entry:
    title = _plain(raw.get("title") or "")
    url = _absolute(_first_link(raw), base_url)

    body_html_raw = _best_content(raw)
    body_html = sanitize_html(body_html_raw, base_url=url or base_url)
    body_text = html_to_text(body_html)

    published_at = _entry_date(raw, ("published", "created", "updated"), fetched_at)
    updated_at = _entry_date(raw, ("updated",), 0) or None
    if updated_at and updated_at < published_at:
        updated_at = None

    guid = _stable_guid(raw, title, body_text, url)
    summary = _summary(raw, body_text)
    enclosure_url, enclosure_type = _enclosure(raw, base_url)

    return Entry(
        feed_id=feed_id,
        guid_hash=hash_guid(feed_id, guid),
        content_hash=hash_content(title, _normalize_for_hash(body_text or summary or "")),
        url=url,
        title=title,
        author=_author(raw),
        summary=summary,
        published_at=published_at,
        updated_at=updated_at,
        fetched_at=fetched_at,
        enclosure_url=enclosure_url,
        enclosure_type=enclosure_type,
        body_html=body_html or None,
        body_text=body_text or None,
    )


def _stable_guid(raw: Any, title: str, body_text: str, url: str | None) -> str:
    """Identidad de la entrada dentro del feed, por orden de preferencia.

    El último recurso mezcla título, fecha *tal cual venía en el XML* y un trozo
    del cuerpo. Deliberadamente no interviene la hora de descarga: si lo hiciera,
    cada refresco crearía entradas nuevas de los mismos artículos.
    """
    for key in ("id", "guid"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if url:
        return url
    raw_date = ""
    for key in ("published", "updated", "created", "date"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            raw_date = value.strip()
            break
    return hash_content(title, raw_date, body_text[:200])


def _first_link(obj: Any) -> str:
    link = obj.get("link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    for item in obj.get("links") or []:
        if item.get("rel") in (None, "alternate") and item.get("href"):
            return str(item["href"]).strip()
    for item in obj.get("links") or []:
        if item.get("href"):
            return str(item["href"]).strip()
    return ""


def _best_content(raw: Any) -> str:
    """El cuerpo más largo disponible: `content` gana a `summary` casi siempre."""
    candidates: list[str] = []
    for item in raw.get("content") or []:
        value = item.get("value")
        if value:
            candidates.append(value)
    for key in ("summary", "description"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    return max(candidates, key=len) if candidates else ""


def _summary(raw: Any, body_text: str) -> str | None:
    """Resumen en texto plano y recortado, para la lista de artículos."""
    source = ""
    for key in ("summary", "description", "subtitle"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            source = value
            break
    text = html_to_text(sanitize_html(source)) if source else body_text
    text = _WS.sub(" ", text.replace("\n", " ")).strip()
    if not text:
        return None
    return _truncate(text, SUMMARY_MAX)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:                        # no partimos una palabra a la mitad
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def _author(raw: Any) -> str | None:
    detail = raw.get("author_detail") or {}
    name = detail.get("name") if isinstance(detail, dict) else None
    candidates = [name, raw.get("author"), raw.get("dc_creator"), raw.get("creator")]
    for item in raw.get("authors") or []:
        if isinstance(item, dict):
            candidates.append(item.get("name"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return _plain(value)[:200]
    return None


def _enclosure(raw: Any, base_url: str) -> tuple[str | None, str | None]:
    """Adjunto principal (audio de un podcast, vídeo, PDF…)."""
    for item in raw.get("enclosures") or []:
        href = item.get("href") or item.get("url")
        if href:
            return _absolute(str(href), base_url), item.get("type") or None
    for item in raw.get("links") or []:
        if item.get("rel") == "enclosure" and item.get("href"):
            return _absolute(str(item["href"]), base_url), item.get("type") or None
    media = raw.get("media_content") or []
    for item in media:
        if isinstance(item, dict) and item.get("url"):
            return _absolute(str(item["url"]), base_url), item.get("type") or None
    return None, None


def _channel_icon(channel: Any, base_url: str) -> str | None:
    image = channel.get("image") or {}
    href = image.get("href") or image.get("url") if isinstance(image, dict) else None
    for value in (href, channel.get("icon"), channel.get("logo")):
        if isinstance(value, str) and value.strip():
            return _absolute(value, base_url)
    return None


# ====================================================================== fechas
def _entry_date(raw: Any, keys: tuple[str, ...], fallback: int) -> int:
    now = now_ms()
    for key in keys:
        ts = _parse_struct(raw.get(f"{key}_parsed")) or _parse_text(raw.get(key))
        if ts is None:
            continue
        if ts <= 0 or ts > now + FUTURE_TOLERANCE_MS:
            # Fecha imposible (año 1900, año 2087, reloj del servidor mal): la
            # tratamos como ausente para no romper el orden de la lista.
            log.debug("fecha descartada por inverosímil: %r", raw.get(key))
            continue
        return ts
    return fallback


def _parse_struct(value: Any) -> int | None:
    if not isinstance(value, time.struct_time):
        return None
    try:
        return calendar.timegm(value) * 1000
    except (ValueError, OverflowError):
        return None


def _parse_text(value: Any) -> int | None:
    """Respaldo con dateutil para los formatos que feedparser no reconoce."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = date_parser.parse(value, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    try:
        if dt.tzinfo is None:                      # sin zona: se asume UTC
            return calendar.timegm(dt.timetuple()) * 1000
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


# ======================================================================== HTML
def sanitize_html(html: str | None, *, base_url: str = "") -> str:
    """Limpia el HTML del cuerpo: sin scripts, sin `on*`, sin píxeles espía."""
    if not html or not html.strip():
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        log.warning("HTML ilegible, se degrada a texto: %s", exc)
        return _plain(html)

    for tag in soup.find_all(list(BLOCKED_TAGS)):
        tag.decompose()

    # Comentarios HTML: esconden marcado de plantilla y no aportan nada al lector.
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        if tag.name == "img" and _is_tracking_pixel(tag):
            tag.decompose()
            continue
        _clean_attrs(tag, base_url)

    return str(soup).strip()


def _clean_attrs(tag: Tag, base_url: str) -> None:
    for name in list(tag.attrs):
        lowered = name.lower()
        if lowered.startswith("on") or lowered in BLOCKED_ATTRS:
            del tag.attrs[name]
            continue
        if lowered in ("href", "src", "poster", "action"):
            value = tag.attrs[name]
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if stripped.lower().startswith(("javascript:", "vbscript:", "data:text/html")):
                del tag.attrs[name]
                continue
            if base_url and stripped:
                tag.attrs[name] = _absolute(stripped, base_url) or stripped


def _is_tracking_pixel(tag: Tag) -> bool:
    """Imagen de 1x1 (o de 0 píxeles): baliza de apertura, no contenido."""
    for dim in ("width", "height"):
        value = tag.get(dim)
        if not isinstance(value, str):
            continue
        limpio = value.strip().rstrip("px").strip()
        if limpio.isdigit() and int(limpio) <= 1:
            return True
    style = tag.get("style")
    if isinstance(style, str) and _ONE_PX.search(style):
        return True
    return not tag.get("src")


def html_to_text(html: str | None) -> str:
    """Texto plano legible: conserva los saltos de párrafo, colapsa el resto."""
    if not html or not html.strip():
        return ""
    if "<" not in html:
        return _collapse(html)
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return _collapse(_plain(html))
    for tag in soup.find_all(list(BLOCKED_TAGS)):
        tag.decompose()
    return _collapse(soup.get_text("\n"))


def _collapse(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.replace("\r", "").split("\n")]
    out = "\n".join(line for line in lines if line)
    return _BLANK_LINES.sub("\n\n", out).strip()


def _plain(value: str) -> str:
    """Quita cualquier marcado y colapsa espacios; para títulos y autores."""
    if not value:
        return ""
    if "<" in value or "&" in value:
        # Un resumen con HTML mal formado no debe impedir guardar el artículo.
        with contextlib.suppress(Exception):
            value = BeautifulSoup(value, "html.parser").get_text(" ")
    return _WS.sub(" ", value.replace("\n", " ")).strip()


def _normalize_for_hash(text: str) -> str:
    """Normaliza el cuerpo antes de la huella: los espacios no son contenido."""
    return _WS.sub(" ", text.replace("\n", " ")).strip()


def _absolute(url: str | None, base_url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not base_url or "://" in url or url.startswith(("mailto:", "tel:", "data:")):
        return url
    try:
        return urljoin(base_url, url)
    except ValueError:
        return url


# ============================================================== descubrimiento
def discover_feed_links(html: str, base_url: str) -> list[str]:
    """Extrae los `<link rel="alternate">` de RSS/Atom de una página HTML."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    wanted = ("application/rss+xml", "application/atom+xml", "application/feed+json",
              "application/json", "text/xml", "application/xml")
    found: list[str] = []
    for link in soup.find_all("link"):
        rels = link.get("rel") or []
        rels = [r.lower() for r in rels] if isinstance(rels, list) else [str(rels).lower()]
        if "alternate" not in rels and "feed" not in rels:
            continue
        mime = str(link.get("type") or "").lower().split(";")[0].strip()
        href = link.get("href")
        if not href or mime not in wanted:
            continue
        resolved = _absolute(str(href), base_url)
        if resolved and resolved not in found:
            found.append(resolved)
    # Los XML puros van primero: preferimos RSS/Atom antes que JSON Feed.
    found.sort(key=lambda u: 0 if u.endswith((".xml", ".rss", ".atom")) else 1)
    return found
