"""Favicons desde el propio sitio o los metadatos del feed, sin servicios terceros."""

from urllib.parse import urljoin

from bs4 import BeautifulSoup
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap

from .resources import decode_image, web_url


class Favicons(QObject):
    disponible = Signal(str)

    def __init__(self, resources, parent=None):
        super().__init__(parent)
        self.resources = resources
        self.icons: dict[str, QIcon] = {}
        self._seen = set()

    def solicitar(self, feed):
        key = (feed.id, feed.site_url, feed.icon_url, feed.url)
        if key in self._seen:
            return
        self._seen.add(key)
        site = feed.site_url or feed.url
        if not web_url(site):
            return
        fallback = urljoin(site, "/favicon.ico")

        def try_icons(urls):
            if not urls:
                return
            url, *rest = urls

            def loaded(data):
                image = decode_image(data)
                if image.isNull():
                    try_icons(rest)
                else:
                    image = image.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
                    self.icons[feed.id] = QIcon(QPixmap.fromImage(image))
                    self.disponible.emit(feed.id)

            self.resources.fetch(url, loaded)

        def page_loaded(data):
            html = data[:512_000] if b"<" in data[:1024] else b""
            soup = BeautifulSoup(html, "html.parser")
            urls = []
            for link in soup.find_all("link", href=True):
                rel = {str(r).lower() for r in link.get("rel", [])}
                if rel.intersection({"icon", "apple-touch-icon"}):
                    url = urljoin(site, link["href"])
                    if web_url(url) and url not in urls:
                        urls.append(url)
            if feed.icon_url and web_url(feed.icon_url):
                urls.append(feed.icon_url)
            try_icons(list(dict.fromkeys([*urls[:4], fallback])))

        QTimer.singleShot(0, self, lambda: self.resources.fetch(site, page_loaded))
