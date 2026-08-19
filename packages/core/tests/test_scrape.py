"""Pruebas del raspado de webs sin feed y de la vigilancia de páginas."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import httpx
import pytest
import respx
from rsscore import repo
from rsscore.config import FetchConfig
from rsscore.db import open_db
from rsscore.ingest import Ingestor, NoFeedFound
from rsscore.models import Feed
from rsscore.scrape import (
    ScrapeConfig,
    ScrapeError,
    WatchConfig,
    guess_selectors,
    looks_javascript_rendered,
    scrape_page,
    watch_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://ana.example/"


def fixture(nombre: str) -> str:
    return (FIXTURES / nombre).read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "scrape.db", device_name="pruebas")


@pytest.fixture
def cfg():
    return FetchConfig(concurrency=4, timeout_seconds=5)


# ============================================================ detección
def test_deduce_el_selector_del_listado():
    candidatos = guess_selectors(fixture("blog_sin_feed.html"), BASE)

    assert candidatos, "debería reconocer el listado de artículos"
    mejor = candidatos[0]
    assert mejor.config.item_selector == "article.post"
    assert mejor.count == 3
    assert "FTS5" in mejor.sample[0]


def test_no_confunde_el_menu_lateral_con_articulos():
    """El `<ul class="menu">` del lateral no es un listado de artículos."""
    candidatos = guess_selectors(fixture("blog_sin_feed.html"), BASE)
    selectores = [c.config.item_selector for c in candidatos]
    assert not any("menu" in s for s in selectores)


def test_detecta_paginas_que_necesitan_javascript():
    assert looks_javascript_rendered(fixture("pagina_javascript.html")) is True
    assert looks_javascript_rendered(fixture("blog_sin_feed.html")) is False


# ============================================================== raspado
def test_extrae_titulo_enlace_y_fecha():
    cfg = guess_selectors(fixture("blog_sin_feed.html"), BASE)[0].config
    parsed = scrape_page(fixture("blog_sin_feed.html"), "FEED1", cfg, base_url=BASE)

    assert len(parsed.entries) == 3
    primera = parsed.entries[0]
    assert primera.title == "Por qué FTS5 me cambió la vida"
    # El enlace relativo tiene que quedar absoluto o no se puede abrir.
    assert primera.url == "https://ana.example/2026/08/fts5"

    # La fecha sale del atributo `datetime`, no del texto que se ve.
    from datetime import datetime

    fecha = datetime.fromtimestamp(primera.published_at / 1000, tz=UTC)
    assert fecha.strftime("%Y-%m-%d") == "2026-08-15"
    # Y el orden del documento se conserva: lo más reciente primero.
    assert primera.published_at > parsed.entries[1].published_at


def test_el_guid_es_estable_entre_raspados():
    """Sin esto, cada refresco insertaría otra vez todos los artículos."""
    cfg = guess_selectors(fixture("blog_sin_feed.html"), BASE)[0].config
    uno = scrape_page(fixture("blog_sin_feed.html"), "FEED1", cfg, base_url=BASE)
    dos = scrape_page(fixture("blog_sin_feed.html"), "FEED1", cfg, base_url=BASE)

    assert [e.guid_hash for e in uno.entries] == [e.guid_hash for e in dos.entries]


def test_selector_que_ya_no_encuentra_nada_da_un_error_util():
    cfg = ScrapeConfig(item_selector="article.post")
    with pytest.raises(ScrapeError) as exc:
        scrape_page(fixture("blog_redisenado.html"), "FEED1", cfg, base_url=BASE)
    assert "cambiado de diseño" in str(exc.value)


def test_pagina_con_javascript_lo_dice_en_vez_de_quedarse_vacia():
    cfg = ScrapeConfig(item_selector="article.post")
    with pytest.raises(ScrapeError) as exc:
        scrape_page(fixture("pagina_javascript.html"), "FEED1", cfg, base_url=BASE)
    assert "JavaScript" in str(exc.value)


def test_selector_css_invalido_se_explica():
    with pytest.raises(ScrapeError) as exc:
        scrape_page(fixture("blog_sin_feed.html"), "F", ScrapeConfig(item_selector="a[["),
                    base_url=BASE)
    assert "no es válido" in str(exc.value)


def test_prefiere_el_enlace_con_mas_texto():
    """En muchas webs el primer <a> de la tarjeta es un icono o un botón."""
    html = """<main><div class="item">
        <a href="/votar" aria-label="votar"><span></span></a>
        <h3><a href="/articulo-real">Un titular suficientemente largo</a></h3>
        <a href="/categoria">cat</a>
      </div><div class="item">
        <a href="/votar2"><span></span></a>
        <h3><a href="/otro-articulo">Otro titular igual de largo</a></h3>
      </div><div class="item">
        <a href="/votar3"><span></span></a>
        <h3><a href="/tercero">Un tercer titular bien largo</a></h3>
      </div></main>"""
    parsed = scrape_page(html, "F", ScrapeConfig(item_selector="div.item"), base_url=BASE)
    assert parsed.entries[0].url == "https://ana.example/articulo-real"


# =========================================================== vigilancia
def test_vigilancia_no_avisa_si_no_cambia_nada():
    cfg = WatchConfig(selector="#contenido")
    html = fixture("pagina_vigilada.html")

    primero, huella = watch_page(html, "F", cfg, base_url=BASE)
    assert len(primero.entries) == 1, "el alta inicial deja constancia"

    segundo, huella2 = watch_page(html, "F", cfg, base_url=BASE, previous_hash=huella)
    assert segundo.entries == []
    assert huella2 == huella


def test_vigilancia_avisa_cuando_cambia():
    cfg = WatchConfig(selector="#contenido")
    original = fixture("pagina_vigilada.html")
    _, huella = watch_page(original, "F", cfg, base_url=BASE)

    cambiada = original.replace("1.4.0 — estable", "1.5.0 — estable")
    parsed, nueva = watch_page(cambiada, "F", cfg, base_url=BASE, previous_hash=huella)

    assert len(parsed.entries) == 1
    assert nueva != huella
    assert "1.5.0" in parsed.entries[0].body_text


def test_ignore_selectors_evita_el_falso_positivo_del_contador():
    """Un contador de visitas cambia en cada visita y dispararía la alarma."""
    cfg = WatchConfig(selector="body", ignore_selectors=[".pie"])
    original = fixture("pagina_vigilada.html")
    _, huella = watch_page(original, "F", cfg, base_url=BASE)

    con_otro_contador = original.replace("Visitas: 48122", "Visitas: 48123")
    parsed, nueva = watch_page(
        con_otro_contador, "F", cfg, base_url=BASE, previous_hash=huella
    )

    assert parsed.entries == [], "solo cambió el pie ignorado"
    assert nueva == huella


def test_zona_vigilada_que_desaparece_da_error_util():
    cfg = WatchConfig(selector="#no-existe")
    with pytest.raises(ScrapeError) as exc:
        watch_page(fixture("pagina_vigilada.html"), "F", cfg, base_url=BASE)
    assert "ya no existe" in str(exc.value)


# ====================================================== integración con ingesta
@respx.mock
async def test_una_web_raspada_recorre_el_mismo_camino_que_un_feed(conn, cfg):
    """Lo raspado tiene que ser indistinguible de lo que viene de un RSS."""
    url = "https://ana.example/"
    respx.get(url).mock(
        return_value=httpx.Response(200, text=fixture("blog_sin_feed.html"),
                                    headers={"Content-Type": "text/html"})
    )
    async with Ingestor(conn, cfg) as ing:
        feed = await ing.add_source(
            url, "scrape", {"item_selector": "article.post", "title_selector": "h2"}
        )

    assert feed.source_kind == "scrape"
    assert conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 3
    # Entra en el índice de búsqueda como cualquier otra entrada.
    assert repo.search(conn, "lamport")
    # Y respeta el intervalo mínimo de cortesía.
    assert feed.interval_seconds >= 1800


@respx.mock
async def test_raspar_dos_veces_no_duplica(conn, cfg):
    url = "https://ana.example/"
    respx.get(url).mock(
        return_value=httpx.Response(200, text=fixture("blog_sin_feed.html"))
    )
    async with Ingestor(conn, cfg) as ing:
        feed = await ing.add_source(url, "scrape", {"item_selector": "article.post"})
        resultado = await ing.refresh_feed(repo.get_feed(conn, feed.id))

    assert resultado.new_entries == []
    assert conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 3


@respx.mock
async def test_el_rediseno_de_la_web_queda_registrado_como_error(conn, cfg):
    url = "https://ana.example/"
    ruta = respx.get(url).mock(
        return_value=httpx.Response(200, text=fixture("blog_sin_feed.html"))
    )
    async with Ingestor(conn, cfg) as ing:
        feed = await ing.add_source(url, "scrape", {"item_selector": "article.post"})
        ruta.mock(return_value=httpx.Response(200, text=fixture("blog_redisenado.html")))
        resultado = await ing.refresh_feed(repo.get_feed(conn, feed.id))

    assert resultado.status == "error"
    guardado = repo.get_feed(conn, feed.id)
    assert "cambiado de diseño" in (guardado.last_error or "")
    # Los artículos que ya estaban no se pierden por un fallo de raspado.
    assert conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 3


@respx.mock
async def test_vigilancia_a_traves_del_motor_de_ingesta(conn, cfg):
    url = "https://descargas.example/"
    ruta = respx.get(url).mock(
        return_value=httpx.Response(200, text=fixture("pagina_vigilada.html"))
    )
    async with Ingestor(conn, cfg) as ing:
        feed = await ing.add_source(url, "watch", {"selector": "#contenido"})
        assert conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 1

        # Sin cambios: nada nuevo.
        await ing.refresh_feed(repo.get_feed(conn, feed.id))
        assert conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"] == 1

        ruta.mock(return_value=httpx.Response(
            200, text=fixture("pagina_vigilada.html").replace("1.4.0", "1.5.0")
        ))
        resultado = await ing.refresh_feed(repo.get_feed(conn, feed.id))

    assert len(resultado.new_entries) == 1
    assert repo.get_feed(conn, feed.id).watch_hash


# =================================================== descubrimiento ampliado
@respx.mock
async def test_encuentra_un_feed_que_la_pagina_no_enlaza(conn, cfg):
    """Muchas webs publican feed sin ponerlo en el <head>."""
    respx.get("https://sitio.example/").mock(
        return_value=httpx.Response(200, text="<html><body>sin link rel</body></html>")
    )
    respx.get("https://sitio.example/feed").mock(return_value=httpx.Response(404))
    respx.get("https://sitio.example/feed/").mock(return_value=httpx.Response(404))
    respx.get("https://sitio.example/rss").mock(return_value=httpx.Response(404))
    respx.get("https://sitio.example/rss.xml").mock(return_value=httpx.Response(404))
    respx.get("https://sitio.example/feed.xml").mock(
        return_value=httpx.Response(
            200, content=(FIXTURES / "rss20.xml").read_bytes(),
            headers={"Content-Type": "application/rss+xml"},
        )
    )
    async with Ingestor(conn, cfg) as ing:
        feed = await ing.add_by_url("https://sitio.example/")

    assert feed.url == "https://sitio.example/feed.xml"
    assert feed.source_kind == "feed", "si hay feed, se prefiere al raspado"


@respx.mock
async def test_sin_feed_se_ofrecen_selectores_para_raspar(conn, cfg):
    respx.get("https://ana.example/").mock(
        return_value=httpx.Response(200, text=fixture("blog_sin_feed.html"))
    )
    respx.route(host="ana.example").mock(return_value=httpx.Response(404))
    respx.get("https://ana.example/").mock(
        return_value=httpx.Response(200, text=fixture("blog_sin_feed.html"))
    )

    async with Ingestor(conn, cfg) as ing:
        with pytest.raises(NoFeedFound) as exc:
            await ing.add_by_url("https://ana.example/")

    assert exc.value.candidates, "debe proponer cómo raspar en vez de rendirse"
    assert exc.value.candidates[0].config.item_selector == "article.post"


# ================================================================ sincronización
def test_el_origen_viaja_entre_dispositivos(tmp_path):
    """Una web raspada dada de alta en el PC debe aparecer en el móvil."""
    from rsscore.sync import apply_ops

    a = open_db(tmp_path / "a.db", device_name="a")
    b = open_db(tmp_path / "b.db", device_name="b")

    feed = Feed(
        url="https://ana.example/", title="Ana", source_kind="scrape",
        source_config_json='{"item_selector": "article.post"}',
    )
    repo.add_feed(a, feed)
    repo.append_change(a, "feed", feed.id, "source_kind", "scrape")
    repo.append_change(
        a, "feed", feed.id, "source_config_json", '{"item_selector": "article.post"}'
    )

    ops, _, _ = repo.changes_since(a, 0, 1000)
    apply_ops(b, ops, record=False)

    copiado = repo.get_feed(b, feed.id)
    assert copiado is not None
    assert copiado.source_kind == "scrape"
    assert "article.post" in copiado.source_config_json
