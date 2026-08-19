"""Exportadores: Obsidian, Kindle y revistas EPUB.

Los tres comparten la limpieza de HTML y la descarga de imágenes de `html.py`, y
los dos que producen libros comparten además el constructor de `epub.py`.

Las importaciones se hacen dentro de cada función a propósito: `epub.py` arrastra
ebooklib y Pillow, y `kindle.py` arrastra aiosmtplib. Quien solo exporta a
Obsidian —el caso del escritorio— no tiene por qué pagar ese arranque.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "build_epub",
    "build_magazine",
    "export_to_obsidian",
    "run_export_job",
    "send_epub_file",
    "send_to_kindle",
    "worker_loop",
]

if TYPE_CHECKING:  # pragma: no cover - solo para los analizadores estáticos
    from .epub import build_epub
    from .jobs import run_export_job, worker_loop
    from .kindle import send_epub_file, send_to_kindle
    from .magazine import build_magazine
    from .obsidian import export_to_obsidian

_ORIGEN = {
    "build_epub": "epub",
    "build_magazine": "magazine",
    "export_to_obsidian": "obsidian",
    "run_export_job": "jobs",
    "send_epub_file": "kindle",
    "send_to_kindle": "kindle",
    "worker_loop": "jobs",
}


def __getattr__(name: str) -> Any:
    """Carga perezosa: `from rsscore.export import X` importa solo lo de X."""
    modulo = _ORIGEN.get(name)
    if modulo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{modulo}", __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
