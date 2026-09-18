"""Recursos HTTP acotados y asíncronos, sin cookies ni credenciales del navegador."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QUrl
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest

MAX_BYTES = 4 * 1024 * 1024


def web_url(value: str) -> bool:
    url = QUrl(value)
    return (url.isValid() and url.scheme() in {"http", "https"}
            and bool(url.host()) and not url.userInfo())


def decode_image(data: bytes) -> QImage:
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    # Comprobar dimensiones ANTES de descomprimir imágenes potencialmente enormes.
    if not size.isValid() or size.width() * size.height() > 16_000_000:
        return QImage()
    reader.setScaledSize(size.scaled(QSize(1600, 1600), Qt.AspectRatioMode.KeepAspectRatio)
                         if max(size.width(), size.height()) > 1600 else size)
    return reader.read()


class RemoteResources(QObject):
    """Hasta cuatro peticiones simultáneas; caché limitada y fallos sin reintento en bucle."""

    def __init__(self, parent=None, cache_dir: Path | None = None):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        if cache_dir is not None:
            cache = QNetworkDiskCache(self)
            cache.setCacheDirectory(str(cache_dir))
            cache.setMaximumCacheSize(32 * 1024 * 1024)
            self.manager.setCache(cache)
        self._waiting: dict[str, list[Callable]] = {}
        self._queue = deque()
        self._active = 0
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_size = 0

    def fetch(self, url: str, callback: Callable[[bytes], None], *, priority=False) -> None:
        if not web_url(url):
            callback(b"")
            return
        if url in self._cache:
            self._cache.move_to_end(url)
            callback(self._cache[url])
            return
        if url in self._waiting:
            self._waiting[url].append(callback)
            return
        self._waiting[url] = [callback]
        if priority:
            self._queue.appendleft(url)
        else:
            self._queue.append(url)
        self._pump()

    def _pump(self):
        while self._queue and self._active < 4:
            url = self._queue.popleft()
            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(12000)
            request.setMaximumRedirectsAllowed(5)
            request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "LectorRSS/0.1")
            for attr in (QNetworkRequest.Attribute.CookieLoadControlAttribute,
                         QNetworkRequest.Attribute.CookieSaveControlAttribute,
                         QNetworkRequest.Attribute.AuthenticationReuseAttribute):
                request.setAttribute(attr, QNetworkRequest.LoadControl.Manual)
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
            reply = self.manager.get(request)
            self._active += 1
            data = bytearray()
            reply.readyRead.connect(lambda r=reply, d=data: self._read(r, d))
            reply.finished.connect(lambda r=reply, d=data, u=url: self._finish(r, d, u))

    @staticmethod
    def _read(reply, data):
        if reply.isOpen() and len(data) <= MAX_BYTES:
            data.extend(bytes(reply.read(MAX_BYTES + 1 - len(data))))
        if len(data) > MAX_BYTES:
            reply.abort()

    def _finish(self, reply, data, url):
        self._read(reply, data)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        payload = bytes(data) if (reply.error() == reply.NetworkError.NoError
                                  and 200 <= status < 300 and len(data) <= MAX_BYTES) else b""
        reply.deleteLater()
        self._active -= 1
        self._cache[url] = payload
        self._cache_size += len(payload)
        while self._cache_size > 12 * 1024 * 1024 or len(self._cache) > 256:
            _, old = self._cache.popitem(last=False)
            self._cache_size -= len(old)
        callbacks = self._waiting.pop(url)
        for callback in callbacks:
            callback(payload)
        self._pump()
