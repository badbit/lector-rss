"""Preferencias del cliente de escritorio y escritura segura del YAML."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import SecretStr
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)
from rsscore.config import Config


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferencias")
        self.setMinimumWidth(520)

        self.device_name = QLineEdit(cfg.device_name)
        self.hub_url = QLineEdit(cfg.hub_url)
        self.hub_url.setPlaceholderText("http://hub.tu-tailnet.ts.net:8787")
        self.hub_token = QLineEdit(cfg.hub_token.get_secret_value())
        self.hub_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.hub_token.setPlaceholderText("Token del hub")
        self.fetch_locally = QCheckBox("Descargar feeds también en este equipo")
        self.fetch_locally.setChecked(cfg.desktop_fetches_locally)

        form = QFormLayout()
        form.addRow("Nombre del dispositivo:", self.device_name)
        form.addRow("Dirección del hub:", self.hub_url)
        form.addRow("Token:", self.hub_token)
        form.addRow("Modo autónomo:", self.fetch_locally)

        explanation = QLabel(
            "Con un hub configurado se recomienda dejar el modo autónomo desactivado: "
            "el hub será el único que visite las fuentes."
        )
        explanation.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(explanation)
        layout.addWidget(buttons)

    def apply_to(self, cfg: Config) -> None:
        cfg.device_name = self.device_name.text().strip()
        cfg.hub_url = self.hub_url.text().strip().rstrip("/")
        cfg.hub_token = SecretStr(self.hub_token.text().strip())
        cfg.desktop.fetch_locally = self.fetch_locally.isChecked()


def save_client_settings(cfg: Config, path: Path) -> None:
    """Actualiza sólo las preferencias del cliente y conserva el resto del YAML."""

    def update(data: dict) -> None:
        data["device_name"] = cfg.device_name
        data["hub_url"] = cfg.hub_url
        data["hub_token"] = cfg.hub_token.get_secret_value()
        _desktop_section(data)["fetch_locally"] = cfg.desktop.fetch_locally

    _update_config_file(path, update)


def save_toolbar_style(style: str, path: Path) -> None:
    """Guarda el aspecto de la barra de herramientas y nada más.

    No pasa por ``save_client_settings``: eso escribiría en el fichero el hub y el
    token aunque en esta sesión vinieran de variables de entorno.
    """
    _update_config_file(path, lambda data: _desktop_section(data).update(toolbar_style=style))


def _desktop_section(data: dict) -> dict:
    desktop = data.setdefault("desktop", {})
    if not isinstance(desktop, dict):
        desktop = data["desktop"] = {}
    return desktop


def _update_config_file(path: Path, update: Callable[[dict], None]) -> None:
    data: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    update(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def edit_settings(parent, cfg: Config, path: Path) -> bool:
    dialog = SettingsDialog(cfg, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    dialog.apply_to(cfg)
    save_client_settings(cfg, path)
    return True
