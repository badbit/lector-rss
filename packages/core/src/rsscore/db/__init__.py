"""Acceso a SQLite: conexión, migraciones, reloj Lamport y compresión de cuerpos.

Se usa sqlite3 directamente en lugar de un ORM: el esquema depende de FTS5
contentless, índices parciales y triggers, que se escriben en SQL puro de todos
modos, y con un archivo de millones de filas interesa controlar exactamente qué
consulta se ejecuta.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import zstandard as zstd

from ..ids import new_id

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_compressor = zstd.ZstdCompressor(level=10)
_decompressor = zstd.ZstdDecompressor()
_lamport_lock = threading.Lock()


# --------------------------------------------------------------------- conexión
def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Abre la base aplicando los PRAGMA que necesita un archivo grande."""
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB
    conn.execute("PRAGMA cache_size = -65536")  # 64 MB
    return conn


def open_db(path: str | Path, *, device_name: str = "") -> sqlite3.Connection:
    """Abre, migra y garantiza que el nodo tiene identidad. Es el punto de entrada."""
    conn = connect(path)
    migrate(conn)
    ensure_node(conn, device_name)
    return conn


# ------------------------------------------------------------------ migraciones
def migrate(conn: sqlite3.Connection) -> int:
    """Aplica las migraciones pendientes. Devuelve la versión resultante."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied = current
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version <= current:
            continue
        sql = f.read_text(encoding="utf-8")
        # executescript hace COMMIT implícito al empezar, así que la transacción
        # tiene que ir dentro del propio script para que la migración sea atómica.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied = version
    return applied


# ------------------------------------------------------------- identidad y reloj
def ensure_node(conn: sqlite3.Connection, device_name: str = "") -> str:
    """Devuelve el device_id de este nodo, creándolo la primera vez."""
    row = conn.execute("SELECT device_id FROM node WHERE id = 1").fetchone()
    if row:
        return row["device_id"]
    device_id = new_id()
    conn.execute(
        "INSERT INTO node (id, device_id, lamport, last_pull_seq) VALUES (1, ?, 0, 0)",
        (device_id,),
    )
    if device_name:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('device_name', ?)",
            (device_name,),
        )
    return device_id


def device_id(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT device_id FROM node WHERE id = 1").fetchone()["device_id"]


def tick_lamport(conn: sqlite3.Connection) -> int:
    """Avanza el reloj lógico para una escritura local."""
    with _lamport_lock:
        conn.execute("UPDATE node SET lamport = lamport + 1 WHERE id = 1")
        return conn.execute("SELECT lamport FROM node WHERE id = 1").fetchone()["lamport"]


def observe_lamport(conn: sqlite3.Connection, remote: int) -> int:
    """Incorpora el reloj de otro dispositivo: max(local, remoto) + 1."""
    with _lamport_lock:
        conn.execute("UPDATE node SET lamport = MAX(lamport, ?) + 1 WHERE id = 1", (remote,))
        return conn.execute("SELECT lamport FROM node WHERE id = 1").fetchone()["lamport"]


# -------------------------------------------------------------------- compresión
def compress(text: str | None) -> bytes | None:
    if text is None:
        return None
    return _compressor.compress(text.encode("utf-8"))


def decompress(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return _decompressor.decompress(blob).decode("utf-8")


# ------------------------------------------------------------------------ utils
def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
