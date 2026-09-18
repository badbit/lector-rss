"""Las pruebas de interfaz nunca descargan recursos de suscripciones reales."""

import pytest


@pytest.fixture(autouse=True)
def aislar_recursos_remotos(monkeypatch, tmp_path, request):
    from rssdesk.resources import RemoteResources

    original = RemoteResources.__init__

    def init(self, parent=None, cache_dir=None):
        original(self, parent, tmp_path / "web-cache")

    monkeypatch.setattr(RemoteResources, "__init__", init)
    if not request.node.get_closest_marker("resource_network"):
        monkeypatch.setattr(RemoteResources, "fetch",
                            lambda self, url, callback, **kwargs: callback(b""))
