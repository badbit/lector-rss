"""Generación de EPUB 3: la base del envío a Kindle y de las revistas.

Decisiones que conviene conocer antes de tocar nada aquí:

* **Estructura plana.** Todo (capítulos, imágenes, hoja de estilo, portada)
  cuelga de la raíz del contenedor, sin subcarpetas. ebooklib no reescribe las
  rutas relativas de los `href`/`src`, así que en cuanto los capítulos viven en
  `text/` y las imágenes en `images/` hay que ir corrigiendo `../` a mano en
  cada referencia; con todo plano, el nombre del fichero *es* la ruta y no hay
  nada que corregir.
* **EPUB 3 con NCX.** Se escriben las dos tablas de contenidos: `nav.xhtml`
  (la de EPUB 3) y `toc.ncx` (la de EPUB 2). El NCX ya no es obligatorio, pero
  los Kindle antiguos y varios lectores baratos siguen leyendo solo esa.
* **Imágenes incrustadas o fuera.** Un EPUB no puede referenciar imágenes
  remotas: el lector no tiene red o el fichero deja de ser autónomo. Las que no
  vengan descargadas como `ImageAsset` se eliminan del documento.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from io import BytesIO
from typing import Any

from bs4 import BeautifulSoup
from ebooklib import epub as _epub

from .. import repo
from ..ids import new_id
from ..models import Entry
from ._render import asset, render
from .html import ImageAsset, clean_article_html, count_words, fetch_images, text_from_html

# Ficheros dentro del contenedor. Planos a propósito (ver el docstring).
CSS_NAME = "estilo.css"
COVER_NAME = "portada.jpg"
TOC_PAGE_NAME = "sumario.xhtml"

WORDS_PER_MINUTE = 220  # lectura tranquila en pantalla; sirve para estimar, no para medir

_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


# ============================================================ modelo de artículo
@dataclass(slots=True)
class EpubArticle:
    """Un artículo listo para entrar en el libro.

    `html` ya viene limpio (`clean_article_html`) y con los `src` de las
    imágenes apuntando al `filename` de su `ImageAsset`.
    """

    title: str
    html: str = ""
    feed: str = ""
    author: str = ""
    url: str = ""
    published: int | None = None          # milisegundos UTC, como en la base
    section: str = ""                     # feed o carpeta; agrupa el índice
    images: list[ImageAsset] = field(default_factory=list)
    entry_id: str = ""

    @property
    def words(self) -> int:
        return count_words(text_from_html(self.html))

    @property
    def date(self) -> dt.datetime | None:
        if self.published is None:
            return None
        return dt.datetime.fromtimestamp(self.published / 1000, tz=dt.UTC)

    def meta_line(self) -> str:
        """Línea de cabecera: feed · autor · fecha, sin separadores huérfanos."""
        partes = [p for p in (self.feed, self.author, format_date(self.date)) if p]
        return " · ".join(partes)


def format_date(value: dt.datetime | dt.date | None) -> str:
    """Fecha en castellano, sin depender de la configuración regional del sistema.

    `locale.setlocale` es global al proceso y no es seguro entre hilos, así que
    no se usa: en un servidor con varios trabajos a la vez cambiaría el idioma
    de otra exportación a media faena.
    """
    if value is None:
        return ""
    return f"{value.day} de {_MESES[value.month - 1]} de {value.year}"


# ==================================================================== portada
def render_cover(
    title: str,
    *,
    subtitle: str = "",
    articles: int = 0,
    size: tuple[int, int] = (1200, 1600),
) -> bytes:
    """Dibuja una portada sobria con Pillow y la devuelve en JPEG.

    Nada de degradados ni de color: el destino es una pantalla de tinta
    electrónica en escala de grises. Papel crema, texto casi negro y dos filetes.
    """
    from PIL import Image, ImageDraw

    ancho, alto = size
    papel, tinta, filete = (244, 241, 234), (26, 26, 26), (120, 120, 120)

    image = Image.new("RGB", size, papel)
    draw = ImageDraw.Draw(image)

    margen = int(ancho * 0.12)
    fuente_titulo = _load_font(int(ancho * 0.085), bold=True)
    fuente_pie = _load_font(int(ancho * 0.035))

    # Filete superior.
    draw.rectangle([margen, int(alto * 0.13), ancho - margen, int(alto * 0.13) + 4], fill=tinta)

    lineas = _wrap(draw, title, fuente_titulo, ancho - 2 * margen)[:6]
    alto_linea = int(ancho * 0.105)
    y = int(alto * 0.22)
    for linea in lineas:
        draw.text((margen, y), linea, font=fuente_titulo, fill=tinta)
        y += alto_linea

    # Filete inferior y pie con fecha y número de artículos.
    y_pie = int(alto * 0.80)
    draw.rectangle([margen, y_pie, ancho - margen, y_pie + 2], fill=filete)
    y_pie += int(alto * 0.03)
    for texto in (subtitle, _plural_articulos(articles) if articles else ""):
        if not texto:
            continue
        draw.text((margen, y_pie), texto, font=fuente_pie, fill=tinta)
        y_pie += int(ancho * 0.055)

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _plural_articulos(n: int) -> str:
    return "1 artículo" if n == 1 else f"{n} artículos"


# Tipografías con serifa habituales en Linux, macOS y Windows, por ese orden.
_FONT_CANDIDATES = {
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
        "/usr/share/fonts/TTF/DejaVuSerif.ttf",
        "/Library/Fonts/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "C:/Windows/Fonts/georgia.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSerif-Bold.ttf",
        "/Library/Fonts/Georgia Bold.ttf",
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "C:/Windows/Fonts/georgiab.ttf",
    ),
}


def _load_font(size: int, *, bold: bool = False) -> Any:
    """Carga una TTF del sistema; si no hay ninguna, la de serie de Pillow.

    Este respaldo no es adorno: en un contenedor mínimo (una imagen `slim` del
    hub) no hay una sola tipografía instalada, y la portada no puede ser el
    motivo de que falle una exportación entera.
    """
    from PIL import ImageFont

    for path in _FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        # Pillow >= 10.1 sabe escalar su tipografía de serie (una TTF que lleva
        # dentro); en versiones anteriores no acepta `size`.
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):
        # Último recurso: la tipografía de mapa de bits, que no necesita nada.
        return ImageFont.load_default()


def _wrap(draw: Any, text: str, font: Any, max_width: int) -> list[str]:
    """Parte el texto en líneas midiendo con la tipografía real."""
    palabras = (text or "").split()
    if not palabras:
        return []
    lineas: list[str] = []
    actual = palabras[0]
    for palabra in palabras[1:]:
        prueba = f"{actual} {palabra}"
        if draw.textlength(prueba, font=font) <= max_width:
            actual = prueba
        else:
            lineas.append(actual)
            actual = palabra
    lineas.append(actual)
    return lineas


# =============================================================== construcción
@dataclass(slots=True)
class _Section:
    """Una sección del índice, tal y como la ve la plantilla del sumario."""

    name: str
    articles: list[dict[str, str]] = field(default_factory=list)


def build_epub(
    articles: Sequence[EpubArticle | Mapping[str, Any]],
    *,
    title: str,
    author: str = "rsscore",
    language: str = "es",
    cover: bytes | None = None,
    css: str | None = None,
    sections: Sequence[str] | Mapping[str, Sequence[Any]] | None = None,
    identifier: str | None = None,
    description: str = "",
    toc_meta: Sequence[tuple[str, str]] | None = None,
    date: dt.date | None = None,
    toc_page: bool = True,
) -> bytes:
    """Arma el EPUB 3 completo y lo devuelve en memoria.

    - `sections`: o bien el orden en el que se quieren las secciones (por
      nombre), o bien directamente el reparto `{sección: [artículos]}`. Si no se
      indica nada se agrupa por `EpubArticle.section` en orden de aparición, y si
      ningún artículo trae sección el índice sale plano.
    - `cover`: JPEG/PNG ya hecho. Si falta se dibuja uno con Pillow.
    - Devuelve `bytes` en lugar de escribir un fichero porque el envío a Kindle
      solo necesita el adjunto y no tiene por qué tocar el disco.
    """
    grupos = _group(articles, sections)
    lista = [a for _, arts in grupos for a in arts]
    hoy = date or dt.date.today()

    book = _epub.EpubBook()
    book.set_identifier(identifier or f"urn:uuid:rsscore-{new_id()}")
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)
    # Metadatos Dublin Core: lo mínimo para que una biblioteca (Calibre, Kobo)
    # ordene el libro sin que haya que editarlo a mano.
    book.add_metadata("DC", "date", hoy.isoformat())
    book.add_metadata("DC", "publisher", "rsscore")
    book.add_metadata("DC", "type", "collection")
    if description:
        book.add_metadata("DC", "description", description)
    for nombre, _ in grupos:
        if nombre:
            book.add_metadata("DC", "subject", nombre)

    estilo = _epub.EpubItem(
        uid="css_principal",
        file_name=CSS_NAME,
        media_type="text/css",
        content=(css or asset("epub_magazine.css")).encode("utf-8"),
    )
    book.add_item(estilo)

    if cover is None:
        cover = render_cover(title, subtitle=format_date(hoy), articles=len(lista))
    book.set_cover(COVER_NAME, cover)

    # Capítulos, en el orden de las secciones.
    por_grupo: list[list[Any]] = []
    todos: list[Any] = []
    vistas: dict[str, ImageAsset] = {}
    indice: list[_Section] = []
    numero = 0
    for nombre, arts in grupos:
        seccion = _Section(name=nombre)
        items: list[Any] = []
        for art in arts:
            numero += 1
            item = _make_chapter(art, numero, language=language, estilo=estilo)
            book.add_item(item)
            items.append(item)
            seccion.articles.append(
                {"href": item.file_name, "title": art.title, "author": art.author}
            )
            for imagen in art.images:
                vistas.setdefault(imagen.filename, imagen)
        por_grupo.append(items)
        todos.extend(items)
        indice.append(seccion)

    for imagen in vistas.values():
        book.add_item(
            _epub.EpubImage(
                uid=imagen.uid,
                file_name=imagen.filename,
                media_type=imagen.media_type,
                content=imagen.data,
            )
        )

    espina: list[Any] = ["cover"]

    if toc_page:
        portadilla = _epub.EpubHtml(
            uid="sumario", file_name=TOC_PAGE_NAME, title="Sumario", lang=language
        )
        portadilla.content = render(
            "epub_toc.xhtml.j2",
            title=title,
            date=format_date(hoy),
            language=language,
            css_href=CSS_NAME,
            meta=list(toc_meta or []),
            sections=indice,
        )
        portadilla.add_item(estilo)
        book.add_item(portadilla)
        espina.append(portadilla)

    # Índice: anidado si hay secciones con nombre, plano si no.
    toc: list[Any] = []
    for (nombre, _), items in zip(grupos, por_grupo, strict=True):
        if nombre:
            toc.append((_epub.Section(nombre), items))
        else:
            toc.extend(items)
    book.toc = toc

    nav = _epub.EpubNav()
    nav.add_item(estilo)
    book.add_item(_epub.EpubNcx())   # compatibilidad con lectores EPUB 2
    book.add_item(nav)

    espina.append(nav)
    espina.extend(todos)
    book.spine = espina

    buf = BytesIO()
    _epub.write_epub(
        buf,
        book,
        {
            # Por omisión ebooklib avisa con un `warning` y devuelve False; así
            # nos enteramos del fallo en vez de quedarnos un EPUB a medias.
            "raise_exceptions": True,
            # `page-list` es para libros con paginación de papel: aquí no hay
            # `epub:type="pagebreak"` en ninguna parte y ebooklib revienta al
            # intentar recorrer la portada, que no tiene cuerpo todavía.
            "epub3_pages": False,
        },
    )
    return buf.getvalue()


def _make_chapter(
    art: EpubArticle, numero: int, *, language: str, estilo: Any
) -> _epub.EpubHtml:
    item = _epub.EpubHtml(
        uid=f"art_{numero:04d}",
        file_name=f"art_{numero:04d}.xhtml",
        title=art.title or f"Artículo {numero}",
        lang=language,
    )
    item.content = render(
        "epub_article.xhtml.j2",
        title=art.title or f"Artículo {numero}",
        meta_line=art.meta_line(),
        body=_prepare_body(art),
        url=art.url,
        language=language,
        css_href=CSS_NAME,
    )
    item.add_item(estilo)
    return item


def _prepare_body(art: EpubArticle) -> str:
    """Deja el HTML del artículo listo para el contenedor.

    Normaliza los `src` al nombre plano del fichero incrustado y borra las
    imágenes que no viajan dentro: un `src` remoto en un EPUB es un hueco roto
    en el lector, y algunos validadores lo dan por inválido.
    """
    if not art.html:
        return "<p>(El artículo no tiene cuerpo; ábrelo en la web.)</p>"
    if not art.images and "<img" not in art.html:
        return art.html

    conocidas = {a.filename for a in art.images}
    soup = BeautifulSoup(art.html, "html.parser")
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        nombre = src.rsplit("/", 1)[-1].split("?", 1)[0]
        if nombre in conocidas:
            img["src"] = nombre
            img.attrs.pop("srcset", None)
        else:
            img.decompose()
    body = soup.body
    return body.decode_contents() if body else str(soup)


def _group(
    articles: Sequence[EpubArticle | Mapping[str, Any]],
    sections: Sequence[str] | Mapping[str, Sequence[Any]] | None,
) -> list[tuple[str, list[EpubArticle]]]:
    """Reparte los artículos en secciones respetando el orden pedido."""
    if isinstance(sections, Mapping):
        return [
            (nombre, [_coerce(a) for a in arts])
            for nombre, arts in sections.items()
            if arts
        ]

    normalizados = [_coerce(a) for a in articles]
    grupos: dict[str, list[EpubArticle]] = {}
    for art in normalizados:
        grupos.setdefault(art.section, []).append(art)

    if sections:
        orden = [s for s in sections if s in grupos]
        orden += [s for s in grupos if s not in orden]
    else:
        orden = list(grupos)
    return [(nombre, grupos[nombre]) for nombre in orden]


def _coerce(article: EpubArticle | Mapping[str, Any]) -> EpubArticle:
    """Acepta también diccionarios: el hub encola trabajos en JSON."""
    if isinstance(article, EpubArticle):
        return article
    campos = {f.name for f in fields(EpubArticle)}
    return EpubArticle(**{k: v for k, v in dict(article).items() if k in campos})


# ================================================ artículos desde la base
async def articles_from_entries(
    conn: sqlite3.Connection,
    entries: Sequence[Entry],
    *,
    client: Any = None,
    embed_images: bool = True,
    max_image_width: int = 1200,
    section_of: Callable[[Entry, str], str] | None = None,
) -> list[EpubArticle]:
    """Convierte entradas de la base en artículos listos para el EPUB.

    Lo comparten el envío a Kindle y la revista, que solo se diferencian en el
    destino del fichero. `section_of` permite agrupar por carpeta en vez de por
    feed sin tocar nada más.
    """
    titulos: dict[str, str] = {}
    salida: list[EpubArticle] = []
    for entry in entries:
        if entry.feed_id not in titulos:
            feed = repo.get_feed(conn, entry.feed_id)
            titulos[entry.feed_id] = feed.display_title if feed else ""
        feed_title = titulos[entry.feed_id]

        html = entry.body_html
        if html is None:
            html, _ = repo.get_body(conn, entry.id)
        html = clean_article_html(html or entry.summary or "", base_url=entry.url)

        imagenes: list[ImageAsset] = []
        if embed_images and "<img" in html:
            html, imagenes = await fetch_images(
                html, client=client, max_width=max_image_width, link_prefix=""
            )

        salida.append(
            EpubArticle(
                title=entry.title or "(sin título)",
                html=html,
                feed=feed_title,
                author=entry.author or "",
                url=entry.url or "",
                published=entry.published_at,
                section=section_of(entry, feed_title) if section_of else feed_title,
                images=imagenes,
                entry_id=entry.id,
            )
        )
    return salida


# ===================================================================== varios
def slugify(text: str, *, max_length: int = 60) -> str:
    """Texto apto para un nombre de fichero ASCII (el adjunto del correo)."""
    limpio = unicodedata.normalize("NFKD", text or "")
    limpio = limpio.encode("ascii", "ignore").decode("ascii").lower()
    limpio = re.sub(r"[^a-z0-9]+", "-", limpio).strip("-")
    return limpio[:max_length].strip("-") or "articulos"


def reading_minutes(words: int) -> int:
    """Minutos estimados de lectura, redondeando hacia arriba."""
    return max(1, -(-words // WORDS_PER_MINUTE)) if words else 0
