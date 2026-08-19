"""Pruebas del motor de ingesta con feeds reales simulados.

Los casos elegidos son los que rompen a los lectores de feeds en la práctica: XML
inválido, fechas imposibles, GUID repetidos y feeds truncados.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from rsscore import repo
from rsscore.config import FetchConfig
from rsscore.db import open_db
from rsscore.ids import now_ms
from rsscore.ingest import Ingestor
from rsscore.models import Feed
from rsscore.parse import parse_feed

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(nombre: str) -> bytes:
    return (FIXTURES / nombre).read_bytes()


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "test.db", device_name="pruebas")


@pytest.fixture
def cfg():
    return FetchConfig(concurrency=4, timeout_seconds=5)


def alta(conn, url: str = "https://ejemplo.org/feed") -> Feed:
    return repo.add_feed(conn, Feed(url=url, title="Prueba"))


# ------------------------------------------------------------------- parseo
def test_parsea_rss20_y_sanea_el_html():
    parsed = parse_feed(fixture("rss20.xml"), "FEED1", base_url="https://ejemplo.org")
    assert parsed.title == "Diario de Ejemplo"
    assert len(parsed.entries) == 2

    primera = parsed.entries[0]
    assert primera.title == "Primera noticia sobre Rust"
    assert "Ana" in (primera.author or "")
    assert "<script>" not in (primera.body_html or ""), "el script debe desaparecer"
    assert "pixel.gif" not in (primera.body_html or ""), "la baliza de 1x1 debe desaparecer"
    assert "formato" in (primera.body_text or "")


def test_parsea_atom_y_resuelve_enlaces_relativos():
    parsed = parse_feed(fixture("atom.xml"), "FEED2", base_url="https://atom.example")
    entrada = parsed.entries[0]
    assert entrada.title == "Entrada Atom con acentós"
    assert entrada.author == "Bruno"
    assert "https://atom.example/rel" in (entrada.body_html or ""), "URL relativa sin resolver"


def test_feed_invalido_no_lanza():
    parsed = parse_feed(fixture("roto.xml"), "FEED3")
    assert isinstance(parsed.entries, list)   # lo que se pueda salvar, se salva


def test_fechas_imposibles_caen_al_instante_de_descarga():
    ahora = now_ms()
    parsed = parse_feed(fixture("fechas_rotas.xml"), "FEED4")
    for entrada in parsed.entries:
        assert entrada.published_at > 0
        # Ninguna fecha puede quedar más de un día en el futuro.
        assert entrada.published_at <= ahora + 86_400_000, entrada.title


def test_guid_duplicado_no_crea_dos_entradas(conn, cfg):
    feed = alta(conn)
    parsed = parse_feed(fixture("guid_duplicado.xml"), feed.id)
    vistos = {e.guid_hash for e in parsed.entries}
    assert len(vistos) == 1 or len(parsed.entries) == 2
    insertadas = 0
    for entrada in parsed.entries:
        if not repo.entry_exists(conn, feed.id, entrada.guid_hash):
            repo.insert_entry(conn, entrada)
            insertadas += 1
    assert insertadas == 1, "dos ítems con el mismo guid son el mismo artículo"


# ------------------------------------------------------------------ ingesta
@respx.mock
async def test_ingesta_completa_y_sin_duplicar(conn, cfg):
    url = "https://ejemplo.org/feed"
    ruta = respx.get(url).mock(
        return_value=httpx.Response(200, content=fixture("rss20.xml"),
                                    headers={"ETag": '"v1"'})
    )
    feed = alta(conn, url)
    async with Ingestor(conn, cfg) as ing:
        primero = await ing.refresh_feed(feed)
        assert primero.status == "ok"
        assert len(primero.new_entries) == 2

        feed = repo.get_feed(conn, feed.id)
        assert feed.etag == '"v1"', "hay que guardar el ETag para el próximo GET"

        segundo = await ing.refresh_feed(feed)
        assert not segundo.new_entries, "reingerir el mismo feed no puede duplicar"
        assert segundo.skipped == 2
    assert ruta.call_count == 2


@respx.mock
async def test_304_no_crea_entradas(conn, cfg):
    url = "https://ejemplo.org/304"
    respx.get(url).mock(return_value=httpx.Response(304))
    feed = repo.add_feed(conn, Feed(url=url, title="Sin cambios", etag='"v1"'))
    async with Ingestor(conn, cfg) as ing:
        resultado = await ing.refresh_feed(feed)
    assert resultado.status == "not_modified"
    assert resultado.new_entries == []


@respx.mock
async def test_contenido_editado_actualiza_en_vez_de_duplicar(conn, cfg):
    url = "https://ejemplo.org/edita"
    v1 = fixture("rss20.xml")
    v2 = v1.replace(b"Un p\xc3\xa1rrafo con", b"Un parrafo CORREGIDO con")
    ruta = respx.get(url).mock(return_value=httpx.Response(200, content=v1))
    feed = alta(conn, url)
    async with Ingestor(conn, cfg) as ing:
        await ing.refresh_feed(feed)
        total_antes = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]

        ruta.mock(return_value=httpx.Response(200, content=v2))
        resultado = await ing.refresh_feed(repo.get_feed(conn, feed.id))

    total_despues = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
    assert total_despues == total_antes, "editar un artículo no debe crear otro"
    assert resultado.updated >= 1


@respx.mock
async def test_error_de_red_no_lanza_y_deja_rastro(conn, cfg):
    url = "https://ejemplo.org/roto"
    respx.get(url).mock(side_effect=httpx.ConnectError("sin ruta al host"))
    feed = alta(conn, url)
    async with Ingestor(conn, cfg) as ing:
        resultado = await ing.refresh_feed(feed)

    assert resultado.status == "error"
    guardado = repo.get_feed(conn, feed.id)
    assert guardado.last_error, "el fallo debe quedar registrado para la interfaz"
    assert guardado.error_count == 1
    assert guardado.next_fetch_at > now_ms(), "tras un error hay que esperar más"


@respx.mock
async def test_el_intervalo_crece_si_no_hay_novedades(conn, cfg):
    url = "https://ejemplo.org/quieto"
    respx.get(url).mock(return_value=httpx.Response(200, content=fixture("rss20.xml")))
    feed = repo.add_feed(conn, Feed(url=url, title="Quieto", interval_seconds=1800))
    async with Ingestor(conn, cfg) as ing:
        await ing.refresh_feed(feed)
        primero = repo.get_feed(conn, feed.id).interval_seconds
        await ing.refresh_feed(repo.get_feed(conn, feed.id))
        segundo = repo.get_feed(conn, feed.id).interval_seconds
    assert segundo >= primero, "sin novedades conviene espaciar las descargas"


@respx.mock
async def test_alta_descubriendo_el_feed_desde_la_pagina(conn, cfg):
    pagina = (
        '<html><head><link rel="alternate" type="application/rss+xml" '
        'href="/feed.xml" title="RSS"></head><body>hola</body></html>'
    )
    respx.get("https://sitio.example/").mock(
        return_value=httpx.Response(200, text=pagina, headers={"Content-Type": "text/html"})
    )
    respx.get("https://sitio.example/feed.xml").mock(
        return_value=httpx.Response(
            200, content=fixture("rss20.xml"),
            headers={"Content-Type": "application/rss+xml"},
        )
    )
    async with Ingestor(conn, cfg) as ing:
        feed = await ing.add_by_url("https://sitio.example/")
    assert feed.url == "https://sitio.example/feed.xml", "debe resolver el feed de la web"
    assert conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 2
