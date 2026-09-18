"""Navegación, imágenes y favicons con eventos Qt y HTTP local controlado."""

import contextlib
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QImage, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from rsscore.models import Entry, Feed
from rsscore.parse import sanitize_html
from rssdesk.article import ArticleView
from rssdesk.favicons import Favicons
from rssdesk.resources import MAX_BYTES, RemoteResources, decode_image


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def png():
    image = QImage(800, 400, QImage.Format.Format_RGB32)
    image.fill(QColor("#e8663d"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def wait_for(predicate):
    for _ in range(150):
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    assert predicate()


def entry(**kwargs):
    return Entry(feed_id="feed", title="Un título <seguro>", guid_hash="g", content_hash="c",
                 url="https://ejemplo.test/noticia?a=1&b=2", **kwargs)


def test_clic_en_titulo_abre_url_original(app, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    view = ArticleView()
    view.resize(700, 400)
    article = entry(body_html="<p>Cuerpo</p>")
    view.mostrar(article)
    view.show()
    app.processEvents()
    found = None
    for y in range(5, 80):
        for x in range(5, 450, 5):
            if view.anchorAt(QPoint(x, y)) == article.url:
                found = QPoint(x, y)
                break
        if found:
            break
    assert found is not None
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=found)
    assert opened == [article.url]
    view._al_pulsar_enlace(QUrl("file:///etc/passwd"))
    assert len(opened) == 1
    view.close()
    view.deleteLater()


def test_imagen_cambia_articulo_bloquea_archivos_y_ajusta_ancho(app):
    callbacks = {}

    class Resources:
        def fetch(self, url, callback, **kwargs):
            callbacks[url] = callback

    view = ArticleView(resources=Resources())
    view.resize(350, 350)
    view.show()
    view.mostrar(entry(body_html='<img src="/vieja.png">'))
    app.processEvents()
    view.loadResource(QTextDocument.ResourceType.ImageResource, QUrl("/vieja.png"))
    view.mostrar(entry(body_html='<img data-src="/nueva.png"><img src="file:///etc/passwd">'))
    app.processEvents()
    view.loadResource(QTextDocument.ResourceType.ImageResource, QUrl("/nueva.png"))
    callbacks["https://ejemplo.test/vieja.png"](png())
    assert not view._images
    callbacks["https://ejemplo.test/nueva.png"](png())
    app.processEvents()
    assert set(view._images) == {"https://ejemplo.test/nueva.png"}
    assert not any(url.startswith("file:") for url in callbacks)
    block = view.document().begin()
    widths = []
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fmt = iterator.fragment().charFormat()
            if fmt.isImageFormat() and "nueva.png" in fmt.toImageFormat().name():
                widths.append(fmt.toImageFormat().width())
            iterator += 1
        block = block.next()
    assert widths and 0 < widths[0] < 350
    assert view.loadResource(QTextDocument.ResourceType.ImageResource,
                             QUrl("file:///etc/passwd")) is None
    view.close()
    view.deleteLater()


def test_lazy_loading_y_srcset_normalizados():
    html = sanitize_html('<img src="placeholder.gif" data-src="/foto.jpg">'
                         '<img srcset="/otra.jpg 800w, /grande.jpg 1600w">',
                         base_url="https://ejemplo.test/articulo")
    assert 'src="https://ejemplo.test/foto.jpg"' in html
    assert 'src="https://ejemplo.test/otra.jpg"' in html
    assert 'src="https://ejemplo.test/tercera.jpg"' in sanitize_html(
        '<img srcset=", , /tercera.jpg 2x">', base_url="https://ejemplo.test/")
    assert decode_image(b"no soy una imagen").isNull()


def test_favicon_descubrimiento_fallback_y_deduplicacion(app):
    calls = []

    class Resources:
        def fetch(self, url, callback):
            calls.append(url)
            if url.endswith("/sitio"):
                callback(b'<link rel="icon" href="/roto.png">')
            else:
                callback(png() if url.endswith("favicon.ico") else b"roto")

    icons = Favicons(Resources())
    feed = Feed(url="https://ejemplo.test/rss", site_url="https://ejemplo.test/sitio")
    icons.solicitar(feed)
    icons.solicitar(feed)
    app.processEvents()
    assert calls == [feed.site_url, "https://ejemplo.test/roto.png",
                     "https://ejemplo.test/favicon.ico"]
    assert not icons.icons[feed.id].isNull()
    icons.deleteLater()


@pytest.mark.resource_network
def test_http_asincrono_cache_limites_y_sin_cookies(app, tmp_path):
    requests = []
    image = png()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Cookie")))
            payload = b"x" * (MAX_BYTES + 1) if self.path == "/large" else image
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", "tracking=1")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resources = RemoteResources(cache_dir=tmp_path / "http")
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        results = []
        resources.fetch(base + "/image", results.append)
        resources.fetch(base + "/image", results.append)
        wait_for(lambda: len(results) == 2)
        assert results == [image, image]
        resources.fetch(base + "/image", results.append)
        assert len(results) == 3
        resources.fetch(base + "/next", results.append)
        wait_for(lambda: len(results) == 4)
        resources.fetch(base + "/large", results.append)
        wait_for(lambda: len(results) == 5)
        assert results[-1] == b""
        resources.fetch("file:///etc/passwd", results.append)
        assert results[-1] == b""
        assert requests == [("/image", None), ("/next", None), ("/large", None)]
    finally:
        resources.deleteLater()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
