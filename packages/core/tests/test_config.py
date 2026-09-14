"""Rutas estables por plataforma y compatibilidad con configuraciones existentes."""

import pytest
from rsscore import config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "RSS_CONFIG", "RSS_DB"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_linux_defaults_are_stable(home, monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.config_home() == home / ".config/rss"
    assert config.Config.load().db_path == home / ".local/share/rss/rss.db"


def test_macos_native_paths(home, monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    native = home / "Library/Application Support/rss"
    assert config.config_home() == native
    assert config.Config.load().db_path == native / "rss.db"


def test_macos_existing_config_is_kept(home, monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    legacy = home / ".config/rss/config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("db_path: ~/old/rss.db\n", encoding="utf-8")
    assert config.default_config_path() == legacy
    assert config.Config.load().db_path == config.Path("~/old/rss.db").expanduser()


def test_environment_overrides_native_paths(home, monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    assert config.config_home() == home / "config/rss"
    assert config.data_home() == home / "data/rss"
    monkeypatch.setenv("RSS_DB", str(home / "override.db"))
    assert config.Config.load().db_path == home / "override.db"


def test_existing_relative_database_is_not_abandoned(home):
    legacy = home / "data/rss.db"
    legacy.parent.mkdir()
    legacy.touch()
    assert config.Config.load().db_path == legacy
