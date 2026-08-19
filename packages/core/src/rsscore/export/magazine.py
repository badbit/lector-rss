"""Revista periódica: un EPUB con lo que haya entrado desde la última vez.

Es el mismo motor que el envío a Kindle, con dos diferencias: se resuelve una
`EntrySelection` en lugar de una lista de identificadores, y el resultado se
escribe en disco con nombre fechado (`revista-2026-08-19.epub`) para que la
biblioteca del lector quede ordenada sola y las ediciones no se pisen.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import repo
from ..config import MagazineConfig, data_home
from ..models import Entry, EntrySelection
from .epub import (
    EpubArticle,
    articles_from_entries,
    build_epub,
    format_date,
    reading_minutes,
    render_cover,
)
from .html import run_sync


@dataclass(slots=True)
class MagazineStats:
    """Las cifras del número, que van en la portada y en la portadilla."""

    articles: int = 0
    words: int = 0
    minutes: int = 0

    def as_meta(self) -> list[tuple[str, str]]:
        """Pares etiqueta/valor tal y como los pinta la portadilla."""
        return [
            ("Artículos", str(self.articles)),
            ("Palabras", f"{self.words:,}".replace(",", ".")),
            ("Lectura", f"{self.minutes} min"),
        ]


@dataclass(slots=True)
class MagazineResult:
    """Lo que devuelve `build_magazine`. `.path` es el EPUB en disco."""

    path: Path
    articles: int = 0
    sections: list[tuple[str, int]] = field(default_factory=list)
    stats: MagazineStats = field(default_factory=MagazineStats)
    size_bytes: int = 0
    skipped: int = 0

    @property
    def name(self) -> str:
        return self.path.name


def magazine_stats(articles: Sequence[EpubArticle]) -> MagazineStats:
    """Artículos, palabras y tiempo estimado de lectura del número."""
    palabras = sum(a.words for a in articles)
    return MagazineStats(
        articles=len(articles), words=palabras, minutes=reading_minutes(palabras)
    )


def build_magazine(
    conn: sqlite3.Connection,
    selection: EntrySelection,
    cfg: MagazineConfig,
    *,
    out_path: Path | str | None = None,
    client: Any = None,
    date: dt.date | None = None,
) -> MagazineResult:
    """Genera el EPUB de la revista y lo deja escrito en disco.

    Es una función síncrona porque la llaman la UI de escritorio y la CLI; la
    descarga de imágenes, que sí es asíncrona, se ejecuta con `run_sync`.
    """
    hoy = date or dt.date.today()
    entradas = _pick(conn, selection, cfg)
    if not entradas:
        raise ValueError("La selección no ha devuelto ningún artículo para la revista")

    articulos = run_sync(
        lambda: articles_from_entries(
            conn,
            entradas,
            client=client,
            embed_images=cfg.embed_images,
            max_image_width=cfg.max_image_width,
        )
    )

    secciones = group_by_feed(articulos)
    stats = magazine_stats(articulos)
    titulo = cfg.title or "Mi revista"

    datos = build_epub(
        articulos,
        title=titulo,
        author=cfg.author,
        language=cfg.language,
        css=cfg.css,
        cover=render_cover(
            titulo, subtitle=format_date(hoy), articles=stats.articles
        ),
        sections=secciones,
        description=(
            f"{stats.articles} artículos de {len(secciones)} fuentes, "
            f"{stats.words} palabras, unos {stats.minutes} minutos de lectura."
        ),
        toc_meta=stats.as_meta(),
        date=hoy,
    )

    destino = _resolve_path(out_path or cfg.output_dir, hoy)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)

    return MagazineResult(
        path=destino,
        articles=stats.articles,
        sections=[(nombre, len(arts)) for nombre, arts in secciones.items()],
        stats=stats,
        size_bytes=len(datos),
        skipped=max(0, len(entradas) - stats.articles),
    )


# --------------------------------------------------------------------- piezas
def _pick(
    conn: sqlite3.Connection, selection: EntrySelection, cfg: MagazineConfig
) -> list[Entry]:
    """Resuelve la selección respetando el tope de artículos de la revista.

    `select_entries` ya ordena por fecha descendente, así que recortar por el
    final deja siempre lo más reciente.
    """
    sel = selection.model_copy()
    if cfg.max_articles > 0:
        sel.limit = min(sel.limit or cfg.max_articles, cfg.max_articles)
    entradas = repo.select_entries(conn, sel)
    if cfg.max_articles > 0:
        entradas = entradas[: cfg.max_articles]
    return list(repo.iter_entries_with_body(conn, entradas))


def group_by_feed(articles: Sequence[EpubArticle]) -> dict[str, list[EpubArticle]]:
    """Agrupa por feed y ordena: secciones alfabéticas, artículos por fecha.

    El orden alfabético de las secciones es deliberado: hace que dos números
    seguidos de la revista se hojeen igual, en vez de bailar según qué feed
    publicó más esa semana.
    """
    grupos: dict[str, list[EpubArticle]] = {}
    for art in articles:
        grupos.setdefault(art.section or art.feed or "Otros", []).append(art)
    return {
        nombre: sorted(arts, key=lambda a: (a.published or 0), reverse=True)
        for nombre, arts in sorted(grupos.items(), key=lambda kv: kv[0].lower())
    }


def _resolve_path(out_path: Path | str | None, day: dt.date) -> Path:
    """`revista-AAAA-MM-DD.epub`, en el directorio pedido o en el de datos."""
    nombre = f"revista-{day.isoformat()}.epub"
    if out_path is None:
        return data_home() / "revistas" / nombre
    destino = Path(out_path).expanduser()
    if destino.is_dir() or destino.suffix.lower() != ".epub":
        return destino / nombre
    return destino
