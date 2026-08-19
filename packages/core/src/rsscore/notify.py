"""Notificaciones: ntfy (móvil vía UnifiedPush) y escritorio.

Dos decisiones importantes viven aquí:

* **Agrupación** (`coalesce`): un refresco puede disparar la misma regla con
  decenas de artículos. Enviar cuarenta avisos hace el sistema inusable, así que
  se agrupan por regla dentro de una ventana temporal y se manda uno solo.
* **Cabeceras ntfy**: HTTP sólo garantiza ASCII en las cabeceras, y los títulos
  en español llevan acentos. Se codifican con RFC 2047 (`=?UTF-8?B?...?=`), que
  es exactamente lo que ntfy sabe decodificar.

El núcleo no depende de `desktop-notifier`: se importa dentro de la función y,
si no está instalado, el canal degrada a un registro en el log.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field

from .config import NotifyConfig
from .ids import now_ms

log = logging.getLogger(__name__)


class Priority(StrEnum):
    LOW = "low"
    DEFAULT = "default"
    HIGH = "high"


_PRIORITY_ORDER: dict[str, int] = {Priority.LOW: 1, Priority.DEFAULT: 2, Priority.HIGH: 3}


class Notification(BaseModel):
    """Un aviso listo para enviar por cualquier canal."""

    title: str = ""
    body: str = ""
    priority: Priority = Priority.DEFAULT
    url: str | None = None
    entry_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    # extras propios de este módulo (no los pide el esquema de reglas, pero sin
    # ellos no se puede agrupar ni fechar la ventana de agrupación)
    rule_name: str | None = None
    count: int = 1
    ts: int = Field(default_factory=now_ms)

    def group_key(self) -> str:
        """Clave de agrupación: la regla que lo disparó, o el título."""
        return self.rule_name or self.title or ""


# ------------------------------------------------------------------- agrupación
def coalesce(
    notifications: Sequence[Notification],
    *,
    window_seconds: int = 300,
    max_items: int = 5,
) -> list[Notification]:
    """Agrupa avisos de la misma regla dentro de una ventana temporal.

    Cuarenta artículos que casan con «Alertas Rust» en el mismo refresco salen
    como un único aviso («40 artículos nuevos coinciden con «Alertas Rust»») con
    los `max_items` primeros titulares como cuerpo.

    Se respeta el orden de entrada: un aviso suelto se devuelve tal cual, sin
    copiarlo.
    """
    if not notifications:
        return []
    window_ms = max(0, int(window_seconds * 1000))
    buckets: list[list[Notification]] = []
    open_bucket: dict[str, int] = {}
    for n in notifications:
        key = n.group_key()
        idx = open_bucket.get(key)
        if idx is not None and abs(n.ts - buckets[idx][0].ts) <= window_ms:
            buckets[idx].append(n)
        else:
            buckets.append([n])
            open_bucket[key] = len(buckets) - 1
    return [b[0] if len(b) == 1 else _merge(b, max_items) for b in buckets]


def _merge(items: list[Notification], max_items: int) -> Notification:
    first = items[0]
    key = first.group_key()
    total = len(items)
    lines = [f"• {i.body or i.title}".rstrip() for i in items[:max_items]]
    if total > max_items:
        lines.append(f"… y {total - max_items} más")
    priority = max(items, key=lambda i: _PRIORITY_ORDER[i.priority]).priority
    tags: list[str] = []
    for i in items:
        for t in i.tags:
            if t not in tags:
                tags.append(t)
    header = (
        f"{total} artículos nuevos coinciden con «{key}»"
        if key
        else f"{total} artículos nuevos"
    )
    return Notification(
        title=first.title or key,
        body="\n".join([header, *lines]),
        priority=priority,
        url=first.url,
        entry_id=None,
        tags=tags,
        rule_name=first.rule_name,
        count=total,
        ts=first.ts,
    )


# ---------------------------------------------------------------------- canales
@runtime_checkable
class Notifier(Protocol):
    """Contrato mínimo de un canal."""

    async def send(self, n: Notification) -> bool: ...


def encode_header(value: str) -> str:
    """Cabecera HTTP segura: ASCII tal cual, el resto en RFC 2047 (base64 UTF-8)."""
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


class NtfyNotifier:
    """POST a un tema de ntfy. Es el canal que llega al móvil por UnifiedPush."""

    def __init__(
        self,
        cfg: NotifyConfig,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.cfg = cfg
        self._client = client
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.ntfy_url)

    def headers(self, n: Notification) -> dict[str, str]:
        h: dict[str, str] = {
            "Title": encode_header(n.title or "RSS"),
            "Priority": str(n.priority),
            "Content-Type": "text/plain; charset=utf-8",
        }
        if n.tags:
            h["Tags"] = encode_header(",".join(n.tags))
        if n.url:
            h["Click"] = n.url
        token = self.cfg.ntfy_token.get_secret_value() if self.cfg.ntfy_token else ""
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def send(self, n: Notification) -> bool:
        if not self.enabled:
            log.debug("ntfy no configurado; se descarta «%s»", n.title)
            return False
        body = (n.body or n.title).encode("utf-8")
        headers = self.headers(n)
        # Que el servidor de avisos esté caído no puede tumbar una ingesta ni un
        # refresco: el canal degrada devolviendo False y se registra el motivo.
        try:
            if self._client is not None:
                resp = await self._client.post(
                    self.cfg.ntfy_url, content=body, headers=headers, timeout=self.timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(self.cfg.ntfy_url, content=body, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("ntfy no aceptó el aviso «%s»: %s", n.title, exc)
            return False
        return True


class DesktopNotifier:
    """Envoltorio perezoso sobre `desktop-notifier`.

    El paquete es opcional (arrastra dependencias de escritorio que el hub
    headless no debe tener), así que se importa dentro de `send` y, si falta, el
    aviso se queda en el log.
    """

    def __init__(self, *, app_name: str = "RSS", enabled: bool = True) -> None:
        self.app_name = app_name
        self.enabled = enabled
        self._impl: Any = None
        self._unavailable = False

    def _load(self) -> Any:
        if self._impl is not None or self._unavailable:
            return self._impl
        try:  # import perezoso: el núcleo no depende de esto
            from desktop_notifier import DesktopNotifier as _Impl
        except ImportError:
            self._unavailable = True
            log.info(
                "desktop-notifier no instalado; las notificaciones de escritorio "
                "se registran en el log"
            )
            return None
        self._impl = _Impl(app_name=self.app_name)
        return self._impl

    async def send(self, n: Notification) -> bool:
        if not self.enabled:
            return False
        impl = self._load()
        if impl is None:
            log.info("[notificación] %s — %s", n.title, n.body.replace("\n", " | "))
            return False
        kwargs: dict[str, Any] = {"title": n.title, "message": n.body}
        urgency = self._urgency(n.priority)
        if urgency is not None:
            kwargs["urgency"] = urgency
        try:
            await impl.send(**kwargs)
        except TypeError:  # firmas distintas entre versiones del paquete
            await impl.send(title=n.title, message=n.body)
        return True

    @staticmethod
    def _urgency(priority: Priority) -> Any:
        try:
            from desktop_notifier import Urgency
        except ImportError:
            return None
        return {
            Priority.LOW: Urgency.Low,
            Priority.DEFAULT: Urgency.Normal,
            Priority.HIGH: Urgency.Critical,
        }.get(priority, Urgency.Normal)


class CompositeNotifier:
    """Envía por todos los canales. Que uno caiga no impide que el otro entregue."""

    def __init__(self, notifiers: Iterable[Any] = ()) -> None:
        self.notifiers = list(notifiers)

    def add(self, notifier: Any) -> None:
        self.notifiers.append(notifier)

    async def send(self, n: Notification) -> int:
        """Devuelve cuántos canales entregaron el aviso."""
        delivered = 0
        for channel in self.notifiers:
            try:
                if await channel.send(n):
                    delivered += 1
            except Exception as exc:
                log.warning("canal %s falló al notificar: %s", type(channel).__name__, exc)
        return delivered

    async def send_many(
        self,
        notifications: Sequence[Notification],
        *,
        window_seconds: int = 300,
        max_items: int = 5,
    ) -> int:
        """Agrupa primero y envía después; es la vía normal desde la ingesta."""
        sent = 0
        for n in coalesce(notifications, window_seconds=window_seconds, max_items=max_items):
            sent += await self.send(n)
        return sent


def build_notifier(cfg: NotifyConfig, *, app_name: str = "RSS") -> CompositeNotifier:
    """Construye los canales que la configuración deja activos."""
    channels: list[Any] = []
    if cfg.ntfy_url:
        channels.append(NtfyNotifier(cfg))
    if cfg.desktop_enabled:
        channels.append(DesktopNotifier(app_name=app_name))
    return CompositeNotifier(channels)
