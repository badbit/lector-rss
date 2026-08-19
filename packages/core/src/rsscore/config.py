"""Configuración: fichero TOML/YAML + variables de entorno.

El hub y el escritorio comparten estructura para que un mismo fichero sirva en
ambos y no haya dos formas de configurar lo mismo.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr


class FetchConfig(BaseModel):
    concurrency: int = 16
    timeout_seconds: float = 30.0
    user_agent: str = "rsscore/0.1 (+https://github.com/local/rss)"
    default_interval_seconds: int = 1800
    max_interval_seconds: int = 86400
    max_body_bytes: int = 8 * 1024 * 1024
    full_text_default: bool = False


class SmtpConfig(BaseModel):
    """Envío a Kindle. Las credenciales nunca salen del hub."""

    host: str = ""
    port: int = 587
    username: str = ""
    password: SecretStr = SecretStr("")
    use_tls: bool = True          # STARTTLS
    use_ssl: bool = False         # SMTPS directo (puerto 465)
    from_address: str = ""        # debe estar aprobado en Amazon
    kindle_address: str = ""      # …@kindle.com


class ObsidianConfig(BaseModel):
    vault_path: Path | None = None
    notes_subdir: str = "Clippings"
    attachments_subdir: str = "Clippings/attachments"
    template: str | None = None   # ruta a una plantilla Jinja2 propia
    download_images: bool = False
    filename_template: str = "{{ date }} - {{ title }}"


class MagazineConfig(BaseModel):
    title: str = "Mi revista"
    author: str = "rsscore"
    language: str = "es"
    output_dir: Path | None = None   # por omisión, data_home()/revistas
    max_articles: int = 200
    embed_images: bool = True
    max_image_width: int = 1200
    css: str | None = None


class NotifyConfig(BaseModel):
    ntfy_url: str = ""            # p.ej. http://hub:8080/rss-alertas
    ntfy_token: SecretStr = SecretStr("")
    desktop_enabled: bool = True


class HubConfig(BaseModel):
    host: str = "127.0.0.1"       # detrás de Tailscale; no exponer a Internet
    port: int = 8787
    tokens: list[SecretStr] = Field(default_factory=list)
    compact_at_hour: int = 4      # compactación nocturna del change_log


class Config(BaseModel):
    db_path: Path = Path("data/rss.db")
    device_name: str = ""
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    magazine: MagazineConfig = Field(default_factory=MagazineConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    hub: HubConfig = Field(default_factory=HubConfig)
    hub_url: str = ""             # que usan los clientes para sincronizar
    hub_token: SecretStr = SecretStr("")

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        path = Path(path or os.environ.get("RSS_CONFIG") or default_config_path())
        data: dict = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = cls.model_validate(data)
        if env_db := os.environ.get("RSS_DB"):
            cfg.db_path = Path(env_db)
        if env_hub := os.environ.get("RSS_HUB_URL"):
            cfg.hub_url = env_hub
        if env_token := os.environ.get("RSS_HUB_TOKEN"):
            cfg.hub_token = SecretStr(env_token)
        cfg.db_path = cfg.db_path.expanduser()
        return cfg


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "rss"


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "rss"


def default_config_path() -> Path:
    return config_home() / "config.yaml"
