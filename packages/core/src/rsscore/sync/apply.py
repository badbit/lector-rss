"""Aplicación de operaciones remotas al estado local.

Es el corazón de la sincronización y tiene que cumplir tres propiedades, que son
las que hacen que dos dispositivos converjan sin coordinarse:

* **Idempotencia** — aplicar el mismo lote dos veces deja el mismo estado.
* **Conmutatividad** — el orden de llegada no afecta al resultado final.
* **Determinismo** — ante el mismo conflicto, todos los nodos eligen lo mismo.

Las tres salen de comparar `(lamport, device_id)` antes de escribir: una
operación solo se aplica si gana a lo que ya hay guardado en ESE campo.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .. import repo
from ..db import observe_lamport
from ..ids import now_ms
from ..models import ChangeOp, Entity, Entry

__all__ = ["ApplyResult", "apply_ops", "replay_pending"]


@dataclass(slots=True)
class ApplyResult:
    applied: int = 0
    ignored: int = 0  # perdió el conflicto: ya había algo más reciente
    pending: int = 0  # la entidad todavía no existe aquí
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.applied} aplicadas, {self.ignored} descartadas por conflicto, "
            f"{self.pending} aparcadas"
        )


# Columnas escribibles por entidad. Es una lista blanca: `field` viene de la red
# y se interpola en el SQL, así que nunca puede salir de aquí.
_FIELDS: dict[Entity, dict[str, str]] = {
    Entity.ENTRY: {"data": "data", "deleted": "deleted"},
    Entity.ENTRY_STATE: {"read": "read", "starred": "starred"},
    Entity.ENTRY_TAG: {"deleted": "deleted"},
    Entity.TAG: {"name": "name", "color": "color", "deleted": "deleted"},
    Entity.FEED: {
        "url": "url",
        "title": "title",
        "custom_title": "custom_title",
        "folder_id": "folder_id",
        "interval_seconds": "interval_seconds",
        "fetch_full_text": "fetch_full_text",
        "disabled": "disabled",
        "deleted": "deleted",
        # El origen se sincroniza: una web raspada dada de alta en el escritorio
        # debe aparecer en el móvil con sus selectores. `watch_hash` no, que es
        # contabilidad local de cada nodo, como el ETag.
        "source_kind": "source_kind",
        "source_config_json": "source_config_json",
    },
    Entity.FOLDER: {
        "name": "name",
        "parent_id": "parent_id",
        "position": "position",
        "deleted": "deleted",
    },
    Entity.RULE: {
        "spec_json": "spec_json",
        "name": "name",
        "enabled": "enabled",
        "position": "position",
        "deleted": "deleted",
    },
    Entity.SAVED_SEARCH: {
        "query": "query",
        "name": "name",
        "filter_json": "filter_json",
        "position": "position",
        "deleted": "deleted",
    },
}

_TABLES: dict[Entity, str] = {
    Entity.ENTRY_STATE: "entry_state",
    Entity.ENTRY_TAG: "entry_tags",
    Entity.TAG: "tags",
    Entity.FEED: "feeds",
    Entity.FOLDER: "folders",
    Entity.RULE: "rules",
    Entity.SAVED_SEARCH: "saved_searches",
}

_BOOL_FIELDS = {"read", "starred", "deleted", "enabled", "disabled", "fetch_full_text"}


def apply_ops(
    conn: sqlite3.Connection, ops: Sequence[ChangeOp], *, record: bool = True
) -> ApplyResult:
    """Aplica un lote de operaciones remotas.

    `record=True` reanota las operaciones aceptadas en el diario local para que el
    hub pueda repartirlas a los demás clientes, pero NUNCA en la cola de subida:
    devolverle al emisor sus propias operaciones haría un bucle infinito.
    """
    result = ApplyResult()
    for op in ops:
        try:
            observe_lamport(conn, op.lamport)
            estado = _apply_one(conn, op)
        except Exception as exc:  # una op corrupta no puede tumbar el lote entero
            result.errors.append(f"{op.entity}/{op.entity_id}/{op.field}: {exc}")
            continue

        if estado == "applied":
            result.applied += 1
            if record:
                repo.append_change(
                    conn,
                    op.entity,
                    op.entity_id,
                    op.field,
                    op.value,
                    lamport=op.lamport,
                    dev=op.device_id,
                    to_outbox=False,
                )
        elif estado == "pending":
            result.pending += 1
            _park(conn, op)
        else:
            result.ignored += 1
    return result


def _apply_one(conn: sqlite3.Connection, op: ChangeOp) -> str:
    columnas = _FIELDS.get(op.entity)
    if not columnas or op.field not in columnas:
        raise ValueError(f"campo no sincronizable: {op.entity}.{op.field}")
    columna = columnas[op.field]
    valor = _coerce(op.field, op.value)

    if op.entity is Entity.ENTRY:
        return _apply_entry(conn, op)

    tabla = _TABLES[op.entity]

    match op.entity:
        case Entity.ENTRY_STATE:
            if not _row_exists(conn, "entries", "id", op.entity_id):
                if repo.field_clock(conn, Entity.ENTRY, op.entity_id, "deleted"):
                    return "ignored"
                return "pending"
            reloj = repo.field_clock(conn, op.entity, op.entity_id, op.field)
            if reloj and not op.wins_over(*reloj):
                return "ignored"
            at = "read_at" if op.field == "read" else "star_at"
            conn.execute(
                f"INSERT INTO entry_state (entry_id, {columna}, {at}, lamport, device_id) "
                "VALUES (?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET "
                f"{columna} = excluded.{columna}, "
                f"{at} = excluded.{at}, lamport = excluded.lamport, device_id = excluded.device_id",
                (op.entity_id, valor, op.ts if valor else None, op.lamport, op.device_id),
            )
            _stamp(conn, op)
            return "applied"

        case Entity.ENTRY_TAG:
            entry_id, _, tag_id = op.entity_id.partition(":")
            if not entry_id or not tag_id:
                raise ValueError("id compuesto inválido, se esperaba 'entry_id:tag_id'")
            if not _row_exists(conn, "entries", "id", entry_id):
                if repo.field_clock(conn, Entity.ENTRY, entry_id, "deleted"):
                    return "ignored"
                return "pending"
            if not _row_exists(conn, "tags", "id", tag_id):
                # La etiqueta llegará en su propia operación; hasta entonces, espera.
                return "pending"
            reloj = repo.field_clock(conn, op.entity, op.entity_id, op.field)
            if reloj and not op.wins_over(*reloj):
                return "ignored"
            conn.execute(
                "INSERT INTO entry_tags (entry_id, tag_id, deleted, lamport, device_id) "
                "VALUES (?,?,?,?,?) ON CONFLICT(entry_id, tag_id) DO UPDATE SET "
                "deleted = excluded.deleted, lamport = excluded.lamport, "
                "device_id = excluded.device_id",
                (entry_id, tag_id, valor, op.lamport, op.device_id),
            )
            _stamp(conn, op)
            return "applied"

    # Resto de entidades: fila propia con (lamport, device_id) y creación al vuelo.
    existe = _row_exists(conn, tabla, "id", op.entity_id)
    reloj = repo.field_clock(conn, op.entity, op.entity_id, op.field)
    if reloj and not op.wins_over(*reloj):
        return "ignored"
    if not existe:
        _create_stub(conn, op.entity, op.entity_id, op.device_id, op.lamport)

    conn.execute(
        f"UPDATE {tabla} SET {columna} = ?, lamport = ?, device_id = ? WHERE id = ?",
        (valor, op.lamport, op.device_id, op.entity_id),
    )
    _stamp(conn, op)
    return "applied"


def _apply_entry(conn: sqlite3.Connection, op: ChangeOp) -> str:
    """Materializa una entrada nueva o el tombstone que la retira."""
    own_clock = repo.field_clock(conn, op.entity, op.entity_id, op.field)
    if own_clock and not op.wins_over(*own_clock):
        return "ignored"

    if op.field == "deleted":
        if not bool(op.value):
            raise ValueError("una entrada eliminada no se puede resucitar sin sus datos")
        repo.delete_entry(conn, op.entity_id, track=False)
        _stamp(conn, op)
        return "applied"

    deleted_clock = repo.field_clock(conn, op.entity, op.entity_id, "deleted")
    if deleted_clock and not op.wins_over(*deleted_clock):
        return "ignored"
    if not isinstance(op.value, dict):
        raise ValueError("entry.data debe ser un objeto")
    data = {**op.value, "id": op.entity_id}
    feed_id = data.get("feed_id")
    if not isinstance(feed_id, str) or not _row_exists(conn, "feeds", "id", feed_id):
        return "pending"
    entry = Entry.model_validate(data)
    if repo.get_entry(conn, op.entity_id, with_body=False):
        # Los metadatos cambiaron pero el cuerpo nuevo no viaja en el diario.
        # Obligar a pedirlo otra vez evita enseñar una versión anterior offline.
        conn.execute("DELETE FROM entry_bodies WHERE entry_id = ?", (op.entity_id,))
        conn.execute("UPDATE entries SET has_body = 0 WHERE id = ?", (op.entity_id,))
        repo.update_entry(conn, op.entity_id, entry, track=False)
    else:
        repo.insert_entry(conn, entry, track=False)
    _stamp(conn, op)
    return "applied"


def _stamp(conn: sqlite3.Connection, op: ChangeOp) -> None:
    """Deja constancia de quién escribió este campo y cuándo, para el próximo
    conflicto. Sin esto, reaplicar el mismo lote volvería a escribir."""
    repo.set_field_clock(conn, op.entity, op.entity_id, op.field, op.lamport, op.device_id)


def _create_stub(
    conn: sqlite3.Connection, entity: Entity, entity_id: str, device: str, lamport: int
) -> None:
    """Crea la fila mínima para poder aplicarle campos.

    Las operaciones de una misma entidad llegan como campos sueltos y en cualquier
    orden, así que la primera que llega tiene que poder materializar la fila.
    """
    ts = now_ms()
    match entity:
        case Entity.FEED:
            conn.execute(
                "INSERT INTO feeds (id, url, title, next_fetch_at, lamport, device_id, updated_at) "
                "VALUES (?, ?, '', 0, ?, ?, ?)",
                (entity_id, f"urn:pendiente:{entity_id}", lamport, device, ts),
            )
        case Entity.FOLDER:
            conn.execute(
                "INSERT INTO folders (id, name, lamport, device_id, updated_at) "
                "VALUES (?,'',?,?,?)",
                (entity_id, lamport, device, ts),
            )
        case Entity.TAG:
            conn.execute(
                "INSERT INTO tags (id, name, lamport, device_id) VALUES (?, ?, ?, ?)",
                (entity_id, f"sin-nombre-{entity_id[-6:]}", lamport, device),
            )
        case Entity.RULE:
            conn.execute(
                "INSERT INTO rules (id, name, enabled, position, spec_json, lamport, device_id, "
                "updated_at) VALUES (?, '', 0, 0, '{}', ?, ?, ?)",
                (entity_id, lamport, device, ts),
            )
        case Entity.SAVED_SEARCH:
            conn.execute(
                "INSERT INTO saved_searches (id, name, query, filter_json, lamport, device_id) "
                "VALUES (?, '', '', '{}', ?, ?)",
                (entity_id, lamport, device),
            )


def _row_exists(conn: sqlite3.Connection, table: str, column: str, value: str) -> bool:
    return (
        conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (value,)).fetchone()
        is not None
    )


def _coerce(field_name: str, value: object) -> object:
    """JSON trae booleanos; SQLite guarda enteros."""
    if field_name in _BOOL_FIELDS:
        return int(bool(value))
    return value


# ------------------------------------------------------- operaciones aparcadas
def _park(conn: sqlite3.Connection, op: ChangeOp) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sync_pending (entity, entity_id, field, value_json, lamport, "
        "device_id, ts) VALUES (?,?,?,?,?,?,?)",
        (
            str(op.entity),
            op.entity_id,
            op.field,
            json.dumps(op.value, ensure_ascii=False),
            op.lamport,
            op.device_id,
            op.ts,
        ),
    )


def replay_pending(conn: sqlite3.Connection, *, entity_ids: Iterable[str] | None = None) -> int:
    """Reintenta las operaciones aparcadas. Se llama tras insertar entradas nuevas.

    Devuelve cuántas se pudieron aplicar por fin.
    """
    sql = "SELECT * FROM sync_pending ORDER BY lamport"
    params: tuple = ()
    ids = list(entity_ids) if entity_ids is not None else None
    if ids:
        # Las ops de etiqueta usan 'entry_id:tag_id', así que se busca por prefijo.
        marcas = " OR ".join(["entity_id = ? OR entity_id LIKE ? || ':%'"] * len(ids))
        sql = f"SELECT * FROM sync_pending WHERE {marcas} ORDER BY lamport"
        params = tuple(x for i in ids for x in (i, i))

    filas = conn.execute(sql, params).fetchall()
    if not filas:
        return 0

    aplicadas = 0
    for fila in filas:
        op = ChangeOp(
            device_id=fila["device_id"],
            lamport=fila["lamport"],
            entity=Entity(fila["entity"]),
            entity_id=fila["entity_id"],
            field=fila["field"],
            value=json.loads(fila["value_json"]),
            ts=fila["ts"],
        )
        try:
            estado = _apply_one(conn, op)
        except Exception:
            conn.execute("DELETE FROM sync_pending WHERE id = ?", (fila["id"],))
            continue
        if estado == "pending":
            conn.execute("UPDATE sync_pending SET tries = tries + 1 WHERE id = ?", (fila["id"],))
            continue
        if estado == "applied":
            aplicadas += 1
            repo.append_change(
                conn,
                op.entity,
                op.entity_id,
                op.field,
                op.value,
                lamport=op.lamport,
                dev=op.device_id,
                to_outbox=False,
            )
        conn.execute("DELETE FROM sync_pending WHERE id = ?", (fila["id"],))
    return aplicadas
