"""Estado compartido del hub: configuración, conexiones y bus de eventos.

SQLite en WAL admite muchos lectores y un escritor. FastAPI ejecuta los endpoints
síncronos en un pool de hilos, así que damos una conexión por hilo y serializamos
las escrituras con un lock: es lo más simple que funciona bien hasta bastante más
carga de la que tendrá un lector personal.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Header, HTTPException, status
from rsscore.config import Config
from rsscore.db import open_db

_local = threading.local()
_write_lock = threading.Lock()
_config: Config | None = None
# Cada `configure()` invalida las conexiones ya abiertas en otros hilos: si no,
# se quedarían apuntando para siempre a la base anterior.
_generation = 0


# ------------------------------------------------------------------ config
def configure(cfg: Config) -> None:
    global _config, _generation
    _config = cfg
    _generation += 1
    # Abre una vez en el hilo principal para aplicar migraciones antes de servir.
    conn = open_db(cfg.db_path, device_name=cfg.device_name or "hub")
    conn.close()


def config() -> Config:
    if _config is None:
        raise RuntimeError("El hub no está configurado; llama a configure() primero")
    return _config


# -------------------------------------------------------------- conexiones
def db() -> sqlite3.Connection:
    """Conexión de este hilo, reabierta si la configuración ha cambiado."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", -1) == _generation:
        return conn
    if conn is not None:
        conn.close()
    conn = open_db(config().db_path, device_name=config().device_name or "hub")
    _local.conn = conn
    _local.generation = _generation
    return conn


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Transacción de escritura serializada."""
    conn = db()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- seguridad
def require_token(authorization: str = Header(default="")) -> str:
    """Autenticación por token. El hub vive tras Tailscale, no expuesto a Internet."""
    cfg = config()
    tokens = [t.get_secret_value() for t in cfg.hub.tokens]
    if not tokens:  # sin tokens configurados: solo uso local
        return "anonymous"
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta la cabecera Authorization")
    token = authorization.removeprefix("Bearer ").strip()
    if token not in tokens:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token inválido")
    return token


# ------------------------------------------------------------- bus de eventos
class EventBus:
    """Reparte novedades a los clientes conectados por SSE."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        """No bloquea nunca: si un cliente va lento, se le descarta el evento."""
        with self._lock:
            subs = list(self._subscribers)
        for queue in subs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


bus = EventBus()
