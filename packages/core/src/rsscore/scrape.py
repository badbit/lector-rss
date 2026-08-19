"""Convertir páginas web en entradas, para sitios que no publican feed.

Dos modos:

* **`scrape`** — la página es un listado de artículos. Con selectores CSS se
  extrae cada entrada con su título, enlace y fecha.
* **`watch`** — la página no es una lista y solo interesa saber cuándo cambia.
  Se hace huella de una zona y cada cambio produce una entrada.

Los dos devuelven un `ParsedFeed`, el mismo contrato que `parse.parse_feed`, así
que a partir de ahí el artículo recorre exactamente el mismo camino que uno de
un feed RSS: deduplicación, texto completo, índice de búsqueda, reglas, alertas,
sincronización y exportación.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field

from .ids import hash_content, hash_guid, now_ms
from .models import Entry
from .parse import ParsedFeed, _absolute, _entry_date, _truncate, html_to_text, sanitize_html

__all__ = [
    "ScrapeCandidate",
    "ScrapeConfig",
    "ScrapeError",
    "WatchConfig",
    "guess_selectors",
    "looks_javascript_rendered",
    "scrape_page",
    "watch_page",
]


class ScrapeError(Exception):
    """Fallo de raspado con un mensaje que le dice al usuario qué hacer."""


# ==================================================================== esquemas
class ScrapeConfig(BaseModel):
    """Selectores CSS que describen dónde está cada cosa en la página."""

    model_config = ConfigDict(extra="forbid")

    item_selector: str                       # cada resultado = un artículo
    title_selector: str = ""                 # vacío = el texto del propio elemento
    # Vacío = se elige el enlace con más texto, que es el titular. Poner "a" por
    # omisión cogería el primero, que en muchas webs es un icono o un botón.
    link_selector: str = ""
    date_selector: str = ""
    date_format: str = ""                    # strptime explícito; si no, tolerante
    summary_selector: str = ""
    author_selector: str = ""
    limit: int = 50


class WatchConfig(BaseModel):
    """Qué zona de la página se vigila y qué ruido se ignora."""

    model_config = ConfigDict(extra="forbid")

    selector: str = ""                       # vacío = la página entera
    # Sin esto, un contador de visitas o un «actualizado el …» dispararía la
    # alarma en cada visita y la vigilancia sería inservible.
    ignore_selectors: list[str] = Field(default_factory=list)
    mode: str = "text"                       # text | html


@dataclass(slots=True)
class ScrapeCandidate:
    """Una propuesta de `item_selector` con muestra de lo que extraería."""

    config: ScrapeConfig
    score: float
    count: int
    sample: list[str] = field(default_factory=list)


# ================================================================== detección
_SCRIPT_RE = re.compile(r"<script\b", re.I)
_APP_ROOT_RE = re.compile(r'<(div|main)[^>]+id=["\'](root|app|__next)["\'][^>]*>\s*</\1>', re.I)


def looks_javascript_rendered(html: str) -> bool:
    """Heurística para distinguir «no hay nada» de «el contenido lo pone el JS».

    Importa para el mensaje de error: decirle al usuario que la página necesita
    un navegador es accionable; dejarle un feed vacío sin explicación, no.
    """
    if not html:
        return False
    if _APP_ROOT_RE.search(html):
        return True
    texto = html_to_text(html)
    scripts = len(_SCRIPT_RE.findall(html))
    return len(texto) < 600 and scripts >= 3


# ==================================================================== raspado
def scrape_page(
    html: str, feed_id: str, cfg: ScrapeConfig, *, base_url: str = ""
) -> ParsedFeed:
    """Extrae las entradas de una página de listado."""
    soup = BeautifulSoup(html or "", "html.parser")
    parsed = ParsedFeed(version="scrape")
    parsed.title = _page_title(soup)
    parsed.site_url = base_url or None

    try:
        elementos = soup.select(cfg.item_selector)
    except Exception as exc:                      # selector CSS mal escrito
        raise ScrapeError(f"el selector «{cfg.item_selector}» no es válido: {exc}") from exc

    if not elementos:
        if looks_javascript_rendered(html):
            raise ScrapeError(
                "la página construye su contenido con JavaScript, así que descargar "
                "el HTML no basta; usa un feed alternativo o un puente tipo RSS-Bridge"
            )
        raise ScrapeError(
            f"el selector «{cfg.item_selector}» no encuentra nada; probablemente "
            "la web ha cambiado de diseño y hay que ajustarlo"
        )

    ahora = now_ms()
    for elemento in elementos[: cfg.limit]:
        try:
            entrada = _build_entry(elemento, feed_id, cfg, base_url, ahora)
        except Exception:                         # un elemento roto no tumba el resto
            continue
        if entrada is not None:
            parsed.entries.append(entrada)

    if not parsed.entries:
        raise ScrapeError(
            f"«{cfg.item_selector}» encontró {len(elementos)} elementos pero ninguno "
            "tenía título aprovechable; revisa title_selector"
        )
    return parsed


def _build_entry(
    elemento: Tag, feed_id: str, cfg: ScrapeConfig, base_url: str, ahora: int
) -> Entry | None:
    titulo = _texto(elemento, cfg.title_selector) if cfg.title_selector else ""
    if not titulo:
        titulo = _titulo_de(elemento) or _texto(elemento, "")
    if not titulo:
        return None

    url = _link(elemento, cfg.link_selector, base_url)
    publicado = _fecha(elemento, cfg, ahora)

    html_limpio = sanitize_html(str(elemento), base_url=base_url)
    texto = html_to_text(html_limpio)
    resumen = _texto(elemento, cfg.summary_selector) if cfg.summary_selector else texto
    autor = _texto(elemento, cfg.author_selector) if cfg.author_selector else None

    # El enlace es la identidad del artículo. Sin un GUID estable cada refresco
    # volvería a insertarlo todo como si fuera nuevo.
    guid = url or f"{titulo}|{publicado}"

    return Entry(
        feed_id=feed_id,
        guid_hash=hash_guid(feed_id, guid),
        content_hash=hash_content(titulo, texto),
        url=url,
        title=_truncate(titulo, 500),
        author=autor or None,
        summary=_truncate(resumen, 500) if resumen else None,
        published_at=publicado,
        fetched_at=ahora,
        body_html=html_limpio,
        body_text=texto,
    )


def _mejor_enlace(elemento: Tag) -> Tag | None:
    """El enlace con más texto, que casi siempre es el titular.

    Coger el primero falla en cuanto la tarjeta lleva delante un icono, una
    flecha de votar o un enlace de categoría: en Hacker News, por ejemplo, el
    primer `<a>` de cada fila es la flecha y su texto está vacío.
    """
    mejor: Tag | None = None
    mejor_largo = -1
    for candidato in elemento.find_all("a", href=True):
        href = str(candidato.get("href") or "")
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        largo = len(candidato.get_text(" ", strip=True))
        if largo > mejor_largo:
            mejor, mejor_largo = candidato, largo
    return mejor


def _texto(elemento: Tag, selector: str) -> str:
    if not selector:
        return elemento.get_text(" ", strip=True)
    try:
        encontrado = elemento.select_one(selector)
    except Exception:
        return ""
    return encontrado.get_text(" ", strip=True) if encontrado else ""


def _link(elemento: Tag, selector: str, base_url: str) -> str | None:
    candidatos: list[Tag] = []
    if selector:
        try:
            candidatos = elemento.select(selector)
        except Exception:
            candidatos = []
    if not candidatos:
        # El propio elemento puede ser el enlace (una tarjeta envuelta en <a>).
        if elemento.name == "a" and elemento.get("href"):
            candidatos = [elemento]
        else:
            mejor = _mejor_enlace(elemento)
            candidatos = [mejor] if mejor is not None else []
    for candidato in candidatos:
        href = candidato.get("href")
        if href and not str(href).startswith(("#", "javascript:", "mailto:")):
            return _absolute(str(href), base_url)
    return None


def _fecha(elemento: Tag, cfg: ScrapeConfig, ahora: int) -> int:
    crudo = ""
    if cfg.date_selector:
        try:
            nodo = elemento.select_one(cfg.date_selector)
        except Exception:
            nodo = None
        if nodo is not None:
            # `<time datetime="...">` es más fiable que el texto que se ve.
            crudo = str(nodo.get("datetime") or nodo.get("content") or
                        nodo.get_text(" ", strip=True))
    if not crudo:
        nodo = elemento.find("time")
        if isinstance(nodo, Tag):
            crudo = str(nodo.get("datetime") or nodo.get_text(" ", strip=True))
    if not crudo:
        return ahora

    if cfg.date_format:
        from datetime import datetime

        try:
            fecha = datetime.strptime(crudo.strip(), cfg.date_format)
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=UTC)
            return int(fecha.timestamp() * 1000)
        except ValueError:
            pass
    # Se reutiliza el mismo criterio que los feeds: fecha inválida o muy futura
    # cae al instante de descarga.
    return _entry_date({"published": crudo}, ("published",), ahora)


def _page_title(soup: BeautifulSoup) -> str:
    for selector in ("meta[property='og:site_name']", "title", "h1"):
        nodo = soup.select_one(selector)
        if nodo is None:
            continue
        valor = str(nodo.get("content") or nodo.get_text(" ", strip=True))
        if valor:
            return _truncate(valor, 200)
    return ""


# ================================================================= vigilancia
def watch_page(
    html: str,
    feed_id: str,
    cfg: WatchConfig,
    *,
    base_url: str = "",
    previous_hash: str | None = None,
) -> tuple[ParsedFeed, str]:
    """Devuelve `(parsed, huella)`. `parsed` trae una entrada solo si cambió."""
    soup = BeautifulSoup(html or "", "html.parser")
    zona: Tag | BeautifulSoup = soup
    if cfg.selector:
        try:
            encontrada = soup.select_one(cfg.selector)
        except Exception as exc:
            raise ScrapeError(f"el selector «{cfg.selector}» no es válido: {exc}") from exc
        if encontrada is None:
            if looks_javascript_rendered(html):
                raise ScrapeError(
                    "la página construye su contenido con JavaScript; el HTML "
                    "descargado no contiene la zona vigilada"
                )
            raise ScrapeError(
                f"la zona vigilada «{cfg.selector}» ya no existe en la página"
            )
        zona = encontrada

    # Quitar el ruido ANTES de la huella, o cada visita parecería un cambio.
    for descartar in cfg.ignore_selectors:
        try:
            for nodo in zona.select(descartar):
                nodo.decompose()
        except Exception:
            continue

    contenido = str(zona) if cfg.mode == "html" else zona.get_text(" ", strip=True)
    contenido = re.sub(r"\s+", " ", contenido).strip()
    huella = hashlib.sha256(contenido.encode("utf-8")).hexdigest()

    parsed = ParsedFeed(version="watch")
    parsed.title = _page_title(soup)
    parsed.site_url = base_url or None

    if previous_hash == huella:
        return parsed, huella

    ahora = now_ms()
    titulo = (
        f"Cambio en {parsed.title or base_url or 'la página vigilada'}"
        if previous_hash is not None
        else f"Vigilancia iniciada: {parsed.title or base_url}"
    )
    parsed.entries.append(
        Entry(
            feed_id=feed_id,
            guid_hash=hash_guid(feed_id, huella),   # cada cambio, una entrada
            content_hash=hash_content(contenido),
            url=base_url or None,
            title=_truncate(titulo, 500),
            summary=_truncate(contenido, 500),
            published_at=ahora,
            fetched_at=ahora,
            body_html=sanitize_html(str(zona), base_url=base_url),
            body_text=contenido,
        )
    )
    return parsed, huella


# ====================================================== detección de selectores
_CONTENEDORES = ("article", "li", "div", "section", "tr")
_TITULO = ("h1", "h2", "h3", "h4", ".title", ".entry-title", ".post-title")


def guess_selectors(html: str, base_url: str = "", *, limit: int = 4) -> list[ScrapeCandidate]:
    """Propone selectores mirando qué estructura se repite en la página.

    Es lo que hace la función usable sin saber CSS: el usuario pega una URL y se
    le enseña qué se extraería antes de dar nada de alta.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    grupos: dict[str, list[Tag]] = {}

    for etiqueta in _CONTENEDORES:
        for elemento in soup.find_all(etiqueta):
            clave = _firma(elemento)
            if clave:
                grupos.setdefault(clave, []).append(elemento)

    candidatos: list[ScrapeCandidate] = []
    for selector, elementos in grupos.items():
        if len(elementos) < 3:            # tres repeticiones ya son un listado
            continue
        puntos, muestra = _puntuar(elementos, base_url)
        if puntos <= 0:
            continue
        candidatos.append(
            ScrapeCandidate(
                config=ScrapeConfig(
                    item_selector=selector,
                    title_selector=_mejor_titulo(elementos),
                    date_selector="time" if _tiene(elementos, "time") else "",
                ),
                score=puntos,
                count=len(elementos),
                sample=muestra,
            )
        )

    candidatos.sort(key=lambda c: c.score, reverse=True)
    return candidatos[:limit]


def _firma(elemento: Tag) -> str:
    """Selector que identifica a un elemento y a sus hermanos del mismo tipo."""
    clases = [c for c in (elemento.get("class") or []) if not _clase_ruidosa(c)]
    if clases:
        return f"{elemento.name}." + ".".join(sorted(clases)[:2])
    padre = elemento.parent
    if isinstance(padre, Tag) and padre.name in ("ul", "ol", "tbody"):
        padre_clases = [c for c in (padre.get("class") or []) if not _clase_ruidosa(c)]
        if padre_clases:
            return f"{padre.name}.{sorted(padre_clases)[0]} > {elemento.name}"
    return ""


def _clase_ruidosa(clase: str) -> bool:
    """Clases generadas al vuelo (CSS-in-JS, hashes) que no sirven de ancla."""
    return bool(re.search(r"\d{3,}|^[a-z]{1,2}$|--|__[a-z0-9]{5,}", clase))


def _titulo_plausible(texto: str) -> bool:
    """Un titular de verdad, no un número de orden ni una etiqueta suelta."""
    return 8 <= len(texto) <= 300


def _titulo_de(elemento: Tag) -> str:
    """Mejor titular del elemento probando todas las vías, no solo la primera.

    Encontrar el selector no basta: en Hacker News cada fila tiene un
    `td.title` que contiene el número de orden («1.»), así que hay que
    comprobar que el texto extraído parezca de verdad un titular y seguir
    buscando si no.
    """
    for selector in _TITULO:
        nodo = elemento.select_one(selector)
        if nodo is None:
            continue
        texto = nodo.get_text(" ", strip=True)
        if _titulo_plausible(texto):
            return texto
    enlace = _mejor_enlace(elemento)
    return enlace.get_text(" ", strip=True) if enlace is not None else ""


def _puntuar(elementos: list[Tag], base_url: str) -> tuple[float, list[str]]:
    con_enlace = con_titulo = con_fecha = 0
    muestra: list[str] = []
    for elemento in elementos[:20]:
        if elemento.find("a", href=True):
            con_enlace += 1
        titulo = _titulo_de(elemento)
        if _titulo_plausible(titulo):
            con_titulo += 1
            if len(muestra) < 3:
                muestra.append(titulo)
        if elemento.find("time"):
            con_fecha += 1

    total = min(len(elementos), 20)
    if not total or not con_titulo:
        return 0.0, muestra
    puntos = (con_enlace / total) * 2 + (con_titulo / total) * 3 + (con_fecha / total)
    puntos *= min(len(elementos), 30) / 30 + 0.5     # premia listados largos
    return puntos, muestra


def _mejor_titulo(elementos: list[Tag]) -> str:
    """Selector del título, o cadena vacía para que `scrape_page` lo deduzca."""
    for selector in _TITULO:
        aciertos = 0
        for e in elementos[:10]:
            nodo = e.select_one(selector)
            if nodo is not None and _titulo_plausible(nodo.get_text(" ", strip=True)):
                aciertos += 1
        if aciertos >= max(2, len(elementos[:10]) // 2):
            return selector
    return ""


def _tiene(elementos: list[Tag], etiqueta: str) -> bool:
    return sum(1 for e in elementos[:10] if e.find(etiqueta)) >= 2


# ==================================================================== fachada
def parse_source(
    html: str, feed_id: str, kind: str, config: dict[str, Any], *,
    base_url: str = "", previous_hash: str | None = None,
) -> tuple[ParsedFeed, str | None]:
    """Punto único de entrada que usa el motor de ingesta."""
    if kind == "scrape":
        return scrape_page(html, feed_id, ScrapeConfig.model_validate(config),
                           base_url=base_url), None
    if kind == "watch":
        return watch_page(html, feed_id, WatchConfig.model_validate(config),
                          base_url=base_url, previous_hash=previous_hash)
    raise ScrapeError(f"origen desconocido: {kind}")
