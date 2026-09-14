"""Instala el acceso de usuario para MATE y otros menús freedesktop.

Ejecutar con el Python del entorno donde esté instalado rssdesk.
Sin --apply únicamente muestra el lanzador y sus destinos.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from rssdesk.icons import ICON_PATH

APP_ID = "org.badbit.LectorRSS"


def exec_arg(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Una ruta del lanzador contiene saltos de línea")
    for source, replacement in (("\\", "\\\\\\\\"), ('"', '\\\\"'),
                                ("`", "\\\\`"), ("$", "\\\\$"), ("%", "%%")):
        value = value.replace(source, replacement)
    return '"' + value + '"'


def desktop_text(executable: Path, workdir: Path) -> str:
    directory = str(workdir).replace("\\", "\\\\")
    return (
        "[Desktop Entry]\nType=Application\nVersion=1.0\n"
        "Name=Lector RSS\nGenericName=Lector de noticias\n"
        "Comment=Lee y organiza noticias, artículos guardados y fuentes RSS\n"
        f"Exec={exec_arg(str(executable))} -m rssdesk.main\n"
        f"Path={directory}\nIcon={APP_ID}\nTerminal=false\n"
        "Categories=Network;News;\nKeywords=RSS;Atom;noticias;Inoreader;\n"
        "StartupNotify=true\nStartupWMClass=rssdesk\nX-LectorRSS-Managed=true\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--data-dir", type=Path, help="directorio XDG alternativo para pruebas")
    args = parser.parse_args(argv)
    root = args.data_dir or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    root = root.expanduser().absolute()
    entry = root / "applications" / f"{APP_ID}.desktop"
    icon = root / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
    license_path = root / "lector-rss" / "LICENSE-icons.txt"
    # No resolver el enlace del intérprete: perdería el entorno virtual.
    executable = Path(sys.executable).absolute()
    workdir = Path(__file__).resolve().parents[1]
    content = desktop_text(executable, workdir)
    print(content)
    print(f"Lanzador: {entry}\nIcono: {icon}")
    if not args.apply:
        print("Previsualización. Añade --apply para instalar.")
        return 0
    if entry.exists() and "X-LectorRSS-Managed=true" not in entry.read_text():
        raise ValueError(f"Ya existe un lanzador ajeno al instalador: {entry}")
    if icon.exists() and not entry.exists() and icon.read_bytes() != ICON_PATH.read_bytes():
        raise ValueError(f"Ya existe un icono diferente: {icon}")
    for path in (entry, icon, license_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON_PATH, icon)
    shutil.copyfile(ICON_PATH.with_name("LICENSE-icons.txt"), license_path)
    entry.write_text(content, encoding="utf-8")
    entry.chmod(0o644)
    if validator := shutil.which("desktop-file-validate"):
        subprocess.run([validator, str(entry)], check=True)
    if updater := shutil.which("update-desktop-database"):
        subprocess.run([updater, str(entry.parent)], check=True)
    if cache := shutil.which("gtk-update-icon-cache"):
        subprocess.run([cache, "--force", "--ignore-theme-index", str(root / "icons/hicolor")],
                       check=False)
    print("Instalado: menú Internet → Lector RSS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
