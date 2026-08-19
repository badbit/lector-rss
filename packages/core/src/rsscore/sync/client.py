"""Cliente de sincronización que usan el escritorio, la CLI y (por HTTP) Android.

Diseñado para trabajar mal conectado: la cola de subida solo se vacía cuando el
hub confirma, y cualquier fallo de red deja el estado local intacto para
reintentarlo más tarde.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass

import httpx

from .. import repo
from ..db import device_id
from ..models import ChangeOp, PullResponse, PushRequest, PushResponse, SyncScope
from .apply import apply_ops, replay_pending
from .snapshot import apply_snapshot

log = logging.getLogger("rsscore.sync")

__all__ = ["SyncClient", "SyncStats"]

LOTE_SUBIDA = 500
LOTE_BAJADA = 2000


@dataclass(slots=True)
class SyncStats:
    subidas: int = 0
    bajadas: int = 0
    aplicadas: int = 0
    descartadas: int = 0
    aparcadas: int = 0
    recuperadas: int = 0
    bootstrap: bool = False

    def __str__(self) -> str:
        base = (
            f"↑{self.subidas} ↓{self.bajadas} · {self.aplicadas} aplicadas, "
            f"{self.descartadas} por conflicto"
        )
        if self.aparcadas:
            base += f", {self.aparcadas} aparcadas"
        if self.recuperadas:
            base += f", {self.recuperadas} recuperadas"
        return "arranque inicial · " + base if self.bootstrap else base


class SyncClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        hub_url: str,
        token: str = "",
        *,
        scope: SyncScope | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.conn = conn
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.scope = scope
        self.timeout = timeout
        self.device_id = device_id(conn)
        self._client = client
        self._propio = client is None

    # ------------------------------------------------------------------ HTTP
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            cabeceras = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            self._client = httpx.AsyncClient(
                base_url=self.hub_url, headers=cabeceras, timeout=self.timeout
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._propio:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> SyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _con_reintentos(self, hacer, *, intentos: int = 3):
        """Backoff exponencial. La cola de subida no se toca hasta confirmar."""
        espera = 1.0
        for intento in range(1, intentos + 1):
            try:
                return await hacer()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if intento == intentos:
                    raise
                log.warning("Sincronización: fallo %s; reintento en %.0fs", exc, espera)
                await asyncio.sleep(espera)
                espera *= 2
        return None

    # ------------------------------------------------------------- registro
    async def register(self, name: str = "") -> None:
        cliente = await self._http()
        cuerpo = {
            "device_id": self.device_id,
            "name": name,
            "scope": (self.scope or SyncScope()).model_dump(),
        }
        r = await cliente.post("/sync/register", json=cuerpo)
        r.raise_for_status()

    # ------------------------------------------------------------------ push
    async def push(self) -> PushResponse:
        cliente = await self._http()
        total = PushResponse()
        while True:
            lote = repo.outbox_batch(self.conn, LOTE_SUBIDA)
            if not lote:
                break
            ids = [i for i, _ in lote]
            ops = [op for _, op in lote]
            peticion = PushRequest(device_id=self.device_id, ops=ops)

            async def enviar(p=peticion):
                r = await cliente.post(
                    "/sync/push",
                    content=p.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
                r.raise_for_status()
                return PushResponse.model_validate(r.json())

            respuesta = await self._con_reintentos(enviar)
            # Solo ahora, con el hub habiendo confirmado, se vacía la cola.
            repo.outbox_clear(self.conn, ids)
            total.accepted += respuesta.accepted
            total.rejected += respuesta.rejected
            total.cursor = respuesta.cursor
            total.server_lamport = respuesta.server_lamport
            if len(lote) < LOTE_SUBIDA:
                break
        return total

    # ------------------------------------------------------------------ pull
    async def pull(self) -> SyncStats:
        cliente = await self._http()
        stats = SyncStats()
        while True:
            desde = self._cursor()

            async def traer(d=desde):
                r = await cliente.get(
                    "/sync/pull",
                    params={"since": d, "limit": LOTE_BAJADA, "device_id": self.device_id},
                )
                r.raise_for_status()
                return PullResponse.model_validate(r.json())

            respuesta = await self._con_reintentos(traer)
            if respuesta.ops:
                resultado = apply_ops(self.conn, respuesta.ops, record=False)
                stats.aplicadas += resultado.applied
                stats.descartadas += resultado.ignored
                stats.aparcadas += resultado.pending
                stats.bajadas += len(respuesta.ops)
            self._set_cursor(respuesta.cursor)
            if not respuesta.has_more:
                break
        return stats

    # ------------------------------------------------------------- bootstrap
    async def bootstrap(self) -> int:
        """Foto inicial: un cliente nuevo no puede reproducir todo el diario."""
        cliente = await self._http()
        params: dict = {"device_id": self.device_id}
        if self.scope and self.scope.days is not None:
            params["days"] = self.scope.days
        r = await cliente.get("/sync/snapshot", params=params, timeout=self.timeout * 4)
        r.raise_for_status()
        return apply_snapshot(self.conn, r.json())

    # ---------------------------------------------------------------- ciclo
    async def sync_once(self, *, name: str = "") -> SyncStats:
        """Un ciclo completo: registro, foto si hace falta, subida, bajada y
        recuperación de las operaciones que llegaron antes de tiempo."""
        try:
            await self.register(name)
        except Exception as exc:
            log.debug("No se pudo registrar el dispositivo: %s", exc)

        stats = SyncStats()
        if self._cursor() == 0:
            try:
                await self.bootstrap()
                stats.bootstrap = True
            except Exception as exc:
                log.warning("Arranque por snapshot fallido, se usará el diario: %s", exc)

        subida = await self.push()
        stats.subidas = subida.accepted

        bajada = await self.pull()
        stats.bajadas = bajada.bajadas
        stats.aplicadas = bajada.aplicadas
        stats.descartadas = bajada.descartadas
        stats.aparcadas = bajada.aparcadas

        stats.recuperadas = replay_pending(self.conn)
        return stats

    # ------------------------------------------------------------------ SSE
    async def stream(self, on_ops, *, reconnect_seconds: float = 5.0) -> None:
        """Escucha el flujo del hub y sincroniza al vuelo. Reconecta solo."""
        while True:
            try:
                cliente = await self._http()
                async with cliente.stream(
                    "GET", "/sync/stream", params={"device_id": self.device_id}, timeout=None
                ) as respuesta:
                    respuesta.raise_for_status()
                    async for linea in respuesta.aiter_lines():
                        if not linea.startswith("data:"):
                            continue
                        try:
                            evento = json.loads(linea[5:].strip() or "{}")
                        except json.JSONDecodeError:
                            continue
                        if evento.get("type") in {"ping", "hello"}:
                            continue
                        stats = await self.pull()
                        await _invoke(on_ops, evento, stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("Flujo de sincronización caído (%s); reconectando", exc)
                await asyncio.sleep(reconnect_seconds)

    # --------------------------------------------------------------- cursor
    def _cursor(self) -> int:
        return self.conn.execute("SELECT last_pull_seq FROM node WHERE id = 1").fetchone()[
            "last_pull_seq"
        ]

    def _set_cursor(self, value: int) -> None:
        self.conn.execute(
            "UPDATE node SET last_pull_seq = MAX(last_pull_seq, ?) WHERE id = 1", (value,)
        )


async def _invoke(callback, evento: dict, stats: SyncStats) -> None:
    if callback is None:
        return
    resultado = callback(evento, stats)
    if asyncio.iscoroutine(resultado):
        await resultado


def ops_from_json(data: list[dict]) -> list[ChangeOp]:
    """Utilidad para clientes que hablan el protocolo sin usar esta clase."""
    return [ChangeOp.model_validate(d) for d in data]
