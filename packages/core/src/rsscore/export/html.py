"""Limpieza de HTML y descarga de imágenes: lo que comparten los tres exportadores.

Un artículo tal y como llega del feed trae de todo: scripts, píxeles de rastreo,
`iframe` de vídeo, atributos `onclick`, URLs relativas al sitio de origen e
imágenes en carga diferida (`data-src`). Nada de eso sobrevive a un EPUB ni tiene
sentido en una nota de Obsidian, así que se normaliza una sola vez aquí y los
exportadores trabajan ya con HTML previsible.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import re
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Tag

T = TypeVar("T")

# Etiquetas que nunca aportan nada a un artículo exportado.
DROP_TAGS = frozenset(
    {
        "script", "style", "noscript", "iframe", "frame", "frameset", "object", "embed",
        "applet", "form", "input", "select", "textarea", "button", "canvas", "svg",
        "link", "meta", "base", "template", "dialog", "audio", "video", "source", "track",
    }
)

# Atributos de presentación o de rastreo que se descartan siempre.
DROP_ATTRS = frozenset(
    {
        "class", "id", "style", "role", "target", "rel", "tabindex", "aria-hidden",
        "aria-label", "aria-labelledby", "aria-describedby", "srcset", "sizes",
        "loading", "decoding", "referrerpolicy", "crossorigin", "sandbox", "ping",
        "itemprop", "itemtype", "itemscope", "contenteditable", "draggable",
    }
)

# Atributos alternativos donde los sitios esconden la imagen real (carga diferida).
LAZY_SRC_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-url", "data-hi-res-src")

# Marcas típicas de balizas de rastreo en la URL de una imagen.
TRACKING_PATTERNS = (
    "doubleclick.net", "google-analytics.com", "googletagmanager.com", "scorecardresearch",
    "feedburner.com/~ff", "feeds.feedburner.com/~", "pixel.wp.com", "stats.wordpress.com",
    "/pixel.gif", "/pixel.png", "/track.gif", "/tracker", "/beacon", "/telemetry",
    "1x1.gif", "spacer.gif", "blank.gif", "sb.scorecardresearch.com",
)

_WS_RE = re.compile(r"[ \t]+")
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.S
)


# ------------------------------------------------------------------ limpieza
def clean_article_html(html: str | None, *, base_url: str | None = None) -> str:
    """Devuelve el HTML del artículo sin scripts, rastreo ni URLs relativas.

    `base_url` es la dirección del artículo: se usa para convertir los enlaces y
    las imágenes relativas en absolutas, porque en un EPUB o en una nota no hay
    ningún documento base contra el que resolverlas.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Comentarios (incluidos los condicionales de anuncios).
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    for tag in soup.find_all(list(DROP_TAGS)):
        tag.decompose()

    for tag in soup.find_all(True):
        _clean_attrs(tag)

    for img in soup.find_all("img"):
        _normalize_img(img, base_url)

    for anchor in soup.find_all("a"):
        _normalize_anchor(anchor, base_url)

    for fig in soup.find_all("figure"):
        _normalize_figure(fig)

    _drop_empty_blocks(soup)

    # `html.parser` conserva `<html>`/`<body>` si venían en la entrada; el
    # exportador quiere solo el fragmento.
    body = soup.body
    out = body.decode_contents() if body else str(soup)
    # Ojo: no se normalizan espacios en blanco. Dentro de <pre> la sangría es
    # significativa y colapsarla destrozaría los bloques de código.
    return out.strip()


def _clean_attrs(tag: Tag) -> None:
    for attr in list(tag.attrs):
        low = attr.lower()
        if low.startswith("on") or low in DROP_ATTRS or low.startswith("data-"):
            # `data-*` de carga diferida ya se ha aprovechado en _normalize_img,
            # que se ejecuta después: se conservan los de las imágenes.
            if tag.name == "img" and low in LAZY_SRC_ATTRS:
                continue
            del tag[attr]


def _normalize_img(img: Tag, base_url: str | None) -> None:
    src = (img.get("src") or "").strip()
    if not src or src.startswith("data:image/svg"):
        for attr in LAZY_SRC_ATTRS:
            if candidate := (img.get(attr) or "").strip():
                src = candidate
                break
    for attr in LAZY_SRC_ATTRS:
        if attr in img.attrs:
            del img[attr]

    if not src or _is_tracking_pixel(img, src):
        _remove_with_container(img)
        return

    img["src"] = _absolutize(src, base_url)
    if not img.get("alt"):
        img["alt"] = ""
    # Las dimensiones en atributos rompen el ajuste al ancho de la pantalla.
    for attr in ("width", "height", "hspace", "vspace", "border", "align"):
        if attr in img.attrs:
            del img[attr]


def _is_tracking_pixel(img: Tag, src: str) -> bool:
    low = src.lower()
    if any(pattern in low for pattern in TRACKING_PATTERNS):
        return True
    for attr in ("width", "height"):
        value = str(img.get(attr) or "").strip().rstrip("px")
        if value.isdigit() and int(value) <= 2:
            return True
    return False


def _normalize_anchor(anchor: Tag, base_url: str | None) -> None:
    href = (anchor.get("href") or "").strip()
    if not href or href.lower().startswith(("javascript:", "vbscript:", "data:")):
        anchor.unwrap()
        return
    anchor["href"] = _absolutize(href, base_url)


def _normalize_figure(fig: Tag) -> None:
    """Deja `figure` con una imagen y, como mucho, un `figcaption`."""
    if not fig.find("img"):
        caption = fig.find("figcaption")
        if caption is None or not caption.get_text(strip=True):
            fig.decompose()
            return
    for nested in fig.find_all("figure"):
        nested.unwrap()


def _remove_with_container(img: Tag) -> None:
    """Quita la imagen y, si su `figure`/`picture` se queda vacío, también."""
    parent = img.parent
    img.decompose()
    while parent is not None and parent.name in ("figure", "picture", "p", "div", "span"):
        if parent.find("img") or parent.get_text(strip=True):
            return
        grandparent = parent.parent
        parent.decompose()
        parent = grandparent


def _drop_empty_blocks(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(["p", "div", "span", "section", "figure", "picture"]):
        if tag.find(["img", "br", "hr", "table"]):
            continue
        if not tag.get_text(strip=True):
            tag.decompose()


def _absolutize(url: str, base_url: str | None) -> str:
    if not base_url:
        return url
    if url.startswith(("data:", "mailto:", "tel:")) or urlparse(url).scheme:
        return url
    return urljoin(base_url, url)


def text_from_html(html: str | None) -> str:
    """Texto plano de un fragmento HTML (para contar palabras y resumir)."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text) if w])


# ------------------------------------------------------------------ imágenes
@dataclass(slots=True)
class ImageAsset:
    """Una imagen ya descargada, redimensionada y lista para incrustar."""

    filename: str
    data: bytes
    media_type: str
    src: str = ""
    width: int = 0
    height: int = 0

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def uid(self) -> str:
        return "img_" + self.filename.rsplit(".", 1)[0]


@dataclass(slots=True)
class FetchStats:
    """Contabilidad de la descarga, útil para el informe de un trabajo."""

    requested: int = 0
    downloaded: int = 0
    failed: list[str] = field(default_factory=list)


async def fetch_images(
    html: str,
    *,
    client: Any = None,
    max_width: int = 1200,
    out_dir: Path | str | None = None,
    link_prefix: str = "",
    max_images: int = 80,
    concurrency: int = 8,
    timeout: float = 20.0,
    stats: FetchStats | None = None,
) -> tuple[str, list[ImageAsset]]:
    """Descarga las imágenes del HTML y reescribe los `src` a rutas locales.

    Cada imagen se reduce a `max_width` conservando la proporción y se recodifica
    a JPEG (o PNG si tiene transparencia). Lo que no se pueda descargar o abrir se
    elimina del documento: es preferible un artículo sin una foto que un EPUB con
    un enlace roto que el lector marca como error.

    Devuelve el HTML reescrito y la lista de imágenes. Si `out_dir` se indica,
    además las escribe en disco; si no, se quedan en memoria (el EPUB las mete en
    el zip sin tocar el disco).
    """
    if not html:
        return "", []

    soup = BeautifulSoup(html, "html.parser")
    imgs = soup.find_all("img")
    if not imgs:
        return html, []

    urls: list[str] = []
    for img in imgs:
        src = (img.get("src") or "").strip()
        if src and src not in urls:
            urls.append(src)
    urls = urls[:max_images]

    stats = stats if stats is not None else FetchStats()
    stats.requested += len(urls)

    own_client = client is None
    if own_client:
        import httpx

        client = httpx.AsyncClient(follow_redirects=True, timeout=timeout)
    try:
        semaphore = asyncio.Semaphore(concurrency)

        async def one(url: str) -> tuple[str, ImageAsset | None]:
            async with semaphore:
                return url, await _fetch_one(url, client=client, max_width=max_width)

        results = await asyncio.gather(*(one(u) for u in urls))
    finally:
        if own_client:
            await client.aclose()

    assets: dict[str, ImageAsset] = {}
    for url, asset in results:
        if asset is None:
            stats.failed.append(url)
            continue
        assets[url] = asset
        stats.downloaded += 1

    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for asset in assets.values():
            (out / asset.filename).write_bytes(asset.data)

    for img in imgs:
        src = (img.get("src") or "").strip()
        asset = assets.get(src)
        if asset is None:
            _remove_with_container(img)
            continue
        img["src"] = f"{link_prefix}{asset.filename}"

    body = soup.body
    out_html = body.decode_contents() if body else str(soup)
    # Se deduplica por nombre: la misma URL repetida no viaja dos veces.
    unique: dict[str, ImageAsset] = {a.filename: a for a in assets.values()}
    return out_html, list(unique.values())


async def _fetch_one(url: str, *, client: Any, max_width: int) -> ImageAsset | None:
    try:
        raw, hint = await _read_bytes(url, client=client)
        if not raw:
            return None
        return _process_image(raw, url=url, max_width=max_width, hint=hint)
    except Exception:
        # Cualquier fallo (red, formato, imagen corrupta) deja la imagen fuera.
        return None


async def _read_bytes(url: str, *, client: Any) -> tuple[bytes, str]:
    if url.startswith("data:"):
        match = _DATA_URI_RE.match(url)
        if not match:
            return b"", ""
        payload = match.group("data")
        try:
            data = base64.b64decode(payload) if match.group("b64") else payload.encode()
        except (binascii.Error, ValueError):
            return b"", ""
        return data, match.group("mime") or ""
    response = await client.get(url)
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "").split(";")[0].strip()


def _process_image(raw: bytes, *, url: str, max_width: int, hint: str = "") -> ImageAsset | None:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(raw)) as image:
        image.load()
        image = ImageOps.exif_transpose(image) or image
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if max_width and image.width > max_width:
            height = max(1, round(image.height * max_width / image.width))
            image = image.resize((max_width, height), Image.LANCZOS)

        buf = BytesIO()
        if has_alpha:
            image.convert("RGBA").save(buf, format="PNG", optimize=True)
            media_type, ext = "image/png", ".png"
        else:
            image.convert("RGB").save(buf, format="JPEG", quality=82, optimize=True)
            media_type, ext = "image/jpeg", ".jpg"
        width, height = image.width, image.height

    data = buf.getvalue()
    if not data:
        return None
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16]
    if hint and not mimetypes.guess_extension(hint):
        hint = ""
    return ImageAsset(
        filename=f"{digest}{ext}",
        data=data,
        media_type=media_type,
        src=url,
        width=width,
        height=height,
    )


# --------------------------------------------------------------------- varios
def run_sync[R](factory: Callable[[], Awaitable[R]]) -> R:
    """Ejecuta una corrutina desde código síncrono, haya o no bucle en marcha.

    Los exportadores de Obsidian y de revistas son síncronos (los llama la UI de
    escritorio), pero la descarga de imágenes es asíncrona. Si ya hay un bucle
    corriendo en este hilo no se puede anidar `asyncio.run`, así que se delega en
    un hilo aparte con su propio bucle.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


def dedupe_assets(groups: Iterable[Iterable[ImageAsset]]) -> list[ImageAsset]:
    """Une varias listas de imágenes descartando las repetidas por nombre."""
    out: dict[str, ImageAsset] = {}
    for group in groups:
        for asset in group:
            out.setdefault(asset.filename, asset)
    return list(out.values())
