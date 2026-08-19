"""Exportación a una bóveda de Obsidian: un fichero Markdown por artículo.

Dos cosas que en esta clase de exportador suelen salir mal y aquí se atacan de
frente:

* **El frontmatter se serializa con `yaml.safe_dump`, nunca a mano.** Un titular
  cualquiera trae dos puntos («Rust 1.80: novedades»), comillas o corchetes, y
  al interpolarlo en `title: {{ title }}` la nota deja de ser YAML válido:
  Obsidian la muestra sin propiedades y Dataview no la ve.
* **Reexportar no duplica.** El nombre sale de una plantilla, así que dos
  artículos distintos pueden querer el mismo fichero. Si el que está en disco es
  *este mismo* artículo (mismo `source`) se sobrescribe; si es otro, se numera
  `-2`, `-3`… Así el mismo artículo exportado tres veces sigue siendo una nota.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from bs4 import BeautifulSoup

from .. import repo
from ..config import ObsidianConfig
from ..models import Entry
from ._render import render, render_file, render_string
from .html import clean_article_html, fetch_images, run_sync

DEFAULT_TEMPLATE = "obsidian_note.md.j2"

# Nombre de fichero. En Linux solo son imposibles `/` y el byte nulo, pero se
# saneen también los que rompen la bóveda en cuanto se sincroniza con Windows,
# Android o iCloud (`: " * ? < > |`), que es lo normal en una bóveda de Obsidian.
# Las comillas y los caracteres de control desaparecen; los separadores de ruta
# se convierten en guion, que sí aporta información visual.
_BORRAR = re.compile(r"[\x00-\x1f\x7f\"'`]")
_SUSTITUIR = re.compile(r"[/\\:*?<>|]")
_ESPACIOS = re.compile(r"\s+")

# Un nombre de fichero cabe en 255 bytes en ext4/btrfs, pero 200 deja sitio para
# el sufijo `-12`, la extensión y los `.sync-conflict-…` que añaden Syncthing y
# compañía.
MAX_FILENAME_BYTES = 200


def export_to_obsidian(
    conn: sqlite3.Connection,
    entry_ids: Iterable[str],
    cfg: ObsidianConfig,
    *,
    client: Any = None,
) -> list[Path]:
    """Escribe una nota por artículo y devuelve las rutas resultantes."""
    if not cfg.vault_path:
        raise ValueError(
            "No hay bóveda configurada: rellena `obsidian.vault_path` en el config.yaml"
        )

    vault = Path(cfg.vault_path).expanduser()
    notes_dir = vault / cfg.notes_subdir if cfg.notes_subdir else vault
    notes_dir.mkdir(parents=True, exist_ok=True)

    attach_dir = vault / cfg.attachments_subdir if cfg.download_images else None
    if attach_dir:
        attach_dir.mkdir(parents=True, exist_ok=True)

    salida: list[Path] = []
    for entry_id in entry_ids:
        entry = repo.get_entry(conn, entry_id, with_body=True)
        if entry is None:
            continue
        salida.append(
            _write_note(conn, entry, cfg, notes_dir=notes_dir, attach_dir=attach_dir, client=client)
        )
    return salida


def _write_note(
    conn: sqlite3.Connection,
    entry: Entry,
    cfg: ObsidianConfig,
    *,
    notes_dir: Path,
    attach_dir: Path | None,
    client: Any = None,
) -> Path:
    feed = repo.get_feed(conn, entry.feed_id)
    feed_title = feed.display_title if feed else ""
    etiquetas = [_tag_name(t.name) for t in repo.entry_tags(conn, entry.id)]

    crudo = entry.body_html or entry.summary or ""
    html = clean_article_html(crudo, base_url=entry.url)
    html = _restore_code_languages(html, _code_languages(crudo))
    if attach_dir is not None and "<img" in html:
        # Enlace relativo desde la carpeta de notas: es lo único que resuelven
        # igual Obsidian de escritorio, el móvil y cualquier editor de texto.
        prefijo = os.path.relpath(attach_dir, notes_dir).replace(os.sep, "/") + "/"
        con_urls = html   # el `lambda` no puede leer la variable que se reasigna
        html, _ = run_sync(
            lambda: fetch_images(con_urls, client=client, out_dir=attach_dir, link_prefix=prefijo)
        )

    body = _to_markdown(html)
    publicado = _iso(entry.published_at)
    meta = _frontmatter(
        entry, feed_title=feed_title, tags=etiquetas, published=publicado
    )

    contexto = {
        "frontmatter": yaml.safe_dump(
            meta, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000
        ),
        "title": entry.title or "(sin título)",
        "body": body,
        "feed": feed_title,
        "author": entry.author or "",
        "source": entry.url or "",
        "published": publicado,
        "published_human": _humano(entry.published_at),
        "tags": etiquetas,
        "entry": entry,
        "meta": meta,
    }
    texto = (
        render_file(cfg.template, **contexto)
        if cfg.template
        else render(DEFAULT_TEMPLATE, **contexto)
    )

    path = _target_path(notes_dir, _filename(entry, cfg, feed_title), meta)
    path.write_text(texto, encoding="utf-8")
    return path


# ------------------------------------------------------------------ frontmatter
def _frontmatter(
    entry: Entry, *, feed_title: str, tags: Sequence[str], published: str
) -> dict[str, Any]:
    """Propiedades de la nota, en el orden en que se quieren leer."""
    titulo = entry.title or "(sin título)"
    return {
        "title": titulo,
        "source": entry.url or "",
        "feed": feed_title,
        "author": entry.author or "",
        "published": published,
        "created": _iso(None),
        "tags": list(tags),
        # El nombre del fichero se sanea y se recorta; el alias conserva el
        # titular real para que la búsqueda de Obsidian siga encontrándolo.
        "aliases": [titulo],
    }


def read_frontmatter(text: str) -> dict[str, Any]:
    """Lee el bloque YAML inicial de una nota. Devuelve `{}` si no lo tiene."""
    if not text.startswith("---"):
        return {}
    partes = text.split("\n---", 2)
    if len(partes) < 2:
        return {}
    try:
        datos = yaml.safe_load(partes[0][3:])
    except yaml.YAMLError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _tag_name(name: str) -> str:
    """Etiqueta válida en Obsidian: sin `#`, sin espacios y sin barras sueltas."""
    limpio = _ESPACIOS.sub("-", (name or "").strip().lstrip("#"))
    return re.sub(r"[^\w/-]", "-", limpio, flags=re.UNICODE).strip("-")


def _iso(ms: int | None) -> str:
    """Marca de tiempo ISO 8601 en UTC (o ahora mismo si no hay fecha)."""
    momento = (
        datetime.now(tz=UTC) if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC)
    )
    return momento.isoformat(timespec="seconds")


def _humano(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------- cuerpo
def _to_markdown(html: str) -> str:
    """HTML limpio a Markdown, conservando código y citas."""
    if not html.strip():
        return ""
    from markdownify import markdownify

    return markdownify(
        html,
        heading_style="ATX",
        bullets="-",
        code_language_callback=_code_language,
        escape_underscores=False,
        escape_asterisks=False,
    ).strip()


def _code_language(element: Any) -> str:
    """Recupera el lenguaje de `class="language-python"` para la valla de código."""
    nodo = element.find("code") if element.name == "pre" else element
    clases = " ".join((nodo.get("class") or []) if nodo is not None else [])
    encontrado = _LANG_RE.search(clases)
    return encontrado.group(1) if encontrado else ""


_LANG_RE = re.compile(r"\b(?:language|lang|highlight|brush:)-?([\w+#.]+)")


def _code_languages(html: str) -> list[str]:
    """Lenguajes de cada `<pre>` del HTML original, en orden de aparición."""
    if "<pre" not in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    salida: list[str] = []
    for pre in soup.find_all("pre"):
        code = pre.find("code")
        clases = " ".join((pre.get("class") or []) + ((code.get("class") or []) if code else []))
        encontrado = _LANG_RE.search(clases)
        salida.append(encontrado.group(1) if encontrado else "")
    return salida


def _restore_code_languages(html: str, langs: list[str]) -> str:
    """Devuelve el `class="language-…"` que la limpieza se llevó por delante.

    `clean_article_html` borra todos los `class` —y hace bien, son estilos del
    sitio de origen—, pero con ellos se va el único sitio donde estaba escrito
    el lenguaje del bloque de código. Sin eso las vallas de Markdown salen
    desnudas y Obsidian no colorea nada. Se vuelven a poner emparejando por
    posición, y solo si el número de bloques cuadra.
    """
    if not any(langs) or "<pre" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    bloques = soup.find_all("pre")
    if len(bloques) != len(langs):
        return html
    for pre, lang in zip(bloques, langs, strict=True):
        code = pre.find("code")
        if lang and code is not None:
            code["class"] = [f"language-{lang}"]
    body = soup.body
    return body.decode_contents() if body else str(soup)


# ------------------------------------------------------------ nombre de fichero
def _filename(entry: Entry, cfg: ObsidianConfig, feed_title: str) -> str:
    """Aplica `filename_template` y lo deja apto para el sistema de ficheros."""
    bruto = render_string(
        cfg.filename_template or "{{ date }} - {{ title }}",
        date=_humano(entry.published_at),
        title=entry.title or "sin-titulo",
        feed=feed_title,
        id=entry.id,
        author=entry.author or "",
    )
    return sanitize_filename(bruto)


def sanitize_filename(name: str, *, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    """Nombre de fichero seguro: sin separadores, sin control y de tamaño acotado."""
    # NFC: Linux guarda los bytes tal cual, así que «á» compuesta y descompuesta
    # serían dos ficheros distintos que se ven igual en pantalla.
    limpio = unicodedata.normalize("NFC", name or "")
    limpio = _BORRAR.sub("", limpio)          # comillas y control: se van sin más
    limpio = _SUSTITUIR.sub("-", limpio)      # separadores de ruta: a guion
    limpio = _ESPACIOS.sub(" ", limpio)
    # Cosmética: que «Rust 1.80: async» no acabe siendo «Rust 1.80- async».
    limpio = re.sub(r"-{2,}", "-", limpio)
    limpio = re.sub(r"\s*-\s*-\s*", " - ", limpio)
    limpio = re.sub(r"(?<=\w)-(?=\s)", "", limpio)
    limpio = _ESPACIOS.sub(" ", limpio).strip(" .")
    if not limpio or limpio in {".", ".."}:
        limpio = "sin-titulo"
    return _truncate_bytes(limpio, max_bytes).strip(" .-") or "sin-titulo"


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Recorta a `max_bytes` sin partir un carácter UTF-8 por la mitad."""
    crudo = text.encode("utf-8")
    if len(crudo) <= max_bytes:
        return text
    return crudo[:max_bytes].decode("utf-8", "ignore")


def _target_path(notes_dir: Path, stem: str, meta: dict[str, Any]) -> Path:
    """Elige el fichero: el mismo si ya es este artículo, o el siguiente libre."""
    for intento in range(1, 1000):
        sufijo = "" if intento == 1 else f"-{intento}"
        base = _truncate_bytes(stem, MAX_FILENAME_BYTES - len(sufijo))
        path = notes_dir / f"{base}{sufijo}.md"
        if not path.exists():
            return path
        if _same_article(path, meta):
            return path
    # Mil colisiones con el mismo nombre: algo va muy mal en la plantilla.
    raise RuntimeError(f"No se encontró un nombre libre para «{stem}» en {notes_dir}")


def _same_article(path: Path, meta: dict[str, Any]) -> bool:
    """¿La nota que hay en disco es este mismo artículo?"""
    try:
        actual = read_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return False
    if not actual:
        return False
    fuente = str(actual.get("source") or "")
    if fuente or meta["source"]:
        return fuente == meta["source"]
    # Sin URL (entradas de feeds que no la traen) queda comparar título y feed.
    return (
        str(actual.get("title") or "") == meta["title"]
        and str(actual.get("feed") or "") == meta["feed"]
    )
