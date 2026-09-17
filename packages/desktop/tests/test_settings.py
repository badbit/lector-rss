from __future__ import annotations

import stat

import yaml
from pydantic import SecretStr
from rsscore.config import Config
from rssdesk.settings import save_client_settings


def test_guardar_preferencias_conserva_el_resto_y_protege_el_token(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("fetch:\n  concurrency: 3\nextra: intacto\n", encoding="utf-8")
    cfg = Config(
        device_name="portátil",
        hub_url="http://hub:8787",
        hub_token=SecretStr("secreto"),
        desktop={"fetch_locally": False},
    )

    save_client_settings(cfg, path)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["fetch"]["concurrency"] == 3
    assert data["extra"] == "intacto"
    assert data["hub_token"] == "secreto"
    assert data["desktop"]["fetch_locally"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_modo_de_descarga_se_deduce_del_hub_si_no_se_configura():
    assert Config(hub_url="").desktop_fetches_locally is True
    assert Config(hub_url="http://hub:8787").desktop_fetches_locally is False
    assert Config(
        hub_url="http://hub:8787", desktop={"fetch_locally": True}
    ).desktop_fetches_locally is True
