"""Acceso a las plantillas Jinja2 que comparten los exportadores.

Vive aparte de `html.py` porque ese módulo no depende de Jinja2 y aquí sí, y
porque el exportador de Obsidian necesita las plantillas sin arrastrar ebooklib.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _autoescape(name: str | None) -> bool:
    """Autoescapa solo el marcado.

    No sirve `select_autoescape`, que mira la última extensión: aquí todas las
    plantillas acaban en `.j2` y lo que decide es la anterior. La nota de
    Obsidian es Markdown, y escapar ahí convertiría cualquier `&` o `<` del
    texto en una entidad HTML dentro de un fichero que no es HTML.
    """
    if not name:
        return False
    stem = name.removesuffix(".j2").removesuffix(".jinja")
    return stem.endswith((".xhtml", ".html", ".xml"))


@lru_cache(maxsize=1)
def env() -> Environment:
    """Entorno Jinja2 con las plantillas del paquete."""
    # El autoescapado no es fijo: lo decide `_autoescape` según la extensión.
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=_autoescape,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render(name: str, **context: Any) -> str:
    """Renderiza una plantilla del paquete."""
    return env().get_template(name).render(**context)


def render_file(path: Path | str, **context: Any) -> str:
    """Renderiza una plantilla del usuario (p. ej. `obsidian.template`)."""
    path = Path(path)
    template = env().from_string(path.read_text(encoding="utf-8"))
    return template.render(**context)


def render_string(source: str, **context: Any) -> str:
    """Renderiza una plantilla corta escrita en la configuración."""
    return env().from_string(source).render(**context)


def asset(name: str) -> str:
    """Devuelve tal cual un fichero de `templates/` (CSS, por ejemplo)."""
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")
