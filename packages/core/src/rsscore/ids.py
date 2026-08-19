"""Identificadores, hashes y tiempo.

Los IDs son ULID: 26 caracteres en base32 de Crockford, ordenables por tiempo de
creación. Esto importa porque los clientes generan IDs estando desconectados y
necesitamos que no colisionen ni desordenen los índices al insertarse.
"""

from __future__ import annotations

import hashlib
import os
import time

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: sin I, L, O, U


def now_ms() -> int:
    """Marca de tiempo actual en milisegundos UTC."""
    return int(time.time() * 1000)


def new_id(ts_ms: int | None = None) -> str:
    """Genera un ULID nuevo."""
    ts = now_ms() if ts_ms is None else ts_ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts << 80) | rand
    out = []
    for shift in range(125, -1, -5):
        out.append(_B32[(value >> shift) & 0x1F])
    return "".join(out)


def ulid_timestamp(ulid: str) -> int:
    """Extrae el instante de creación (ms) de un ULID."""
    value = 0
    for ch in ulid[:10]:
        value = value * 32 + _B32.index(ch.upper())
    return value


def hash_guid(feed_id: str, guid: str) -> str:
    """Identidad estable de una entrada dentro de su feed."""
    return hashlib.sha256(f"{feed_id}\x00{guid}".encode()).hexdigest()


def hash_content(*parts: str | None) -> str:
    """Huella del contenido: detecta ediciones del artículo y duplicados cruzados."""
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").strip().casefold().encode())
        h.update(b"\x00")
    return h.hexdigest()
