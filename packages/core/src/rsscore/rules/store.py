"""Persistencia de reglas.

Las reglas se sincronizan entre dispositivos como cualquier otra entidad: cada
alta o cambio se anota en el diario con `append_change`, así que crear una regla
en el escritorio la hace aparecer en el móvil.
"""

from __future__ import annotations

import sqlite3

import yaml

from .. import repo
from ..db import device_id, tick_lamport
from ..ids import now_ms
from ..models import Entity
from .models import Rule, parse_rules

__all__ = [
    "delete_rule",
    "export_rules_yaml",
    "get_rule",
    "import_rules_yaml",
    "load_rules",
    "save_rule",
]


def load_rules(conn: sqlite3.Connection, *, include_disabled: bool = False) -> list[Rule]:
    sql = "SELECT * FROM rules WHERE deleted = 0"
    if not include_disabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY position, name"
    reglas: list[Rule] = []
    for row in conn.execute(sql):
        rule = _row_to_rule(row)
        if rule is not None:
            reglas.append(rule)
    return reglas


def get_rule(conn: sqlite3.Connection, rule_id: str) -> Rule | None:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return _row_to_rule(row) if row else None


def save_rule(conn: sqlite3.Connection, rule: Rule, *, track: bool = True) -> Rule:
    lam = tick_lamport(conn)
    dev = device_id(conn)
    spec = rule.model_dump_json()
    conn.execute(
        "INSERT INTO rules (id, name, enabled, position, spec_json, deleted, lamport, "
        "device_id, updated_at) VALUES (?,?,?,?,?,0,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled, "
        "position=excluded.position, spec_json=excluded.spec_json, deleted=0, "
        "lamport=excluded.lamport, device_id=excluded.device_id, updated_at=excluded.updated_at",
        (rule.id, rule.name, int(rule.enabled), rule.position, spec, lam, dev, now_ms()),
    )
    if track:
        repo.append_change(conn, Entity.RULE, rule.id, "spec_json", spec, lamport=lam, dev=dev)
        repo.append_change(
            conn, Entity.RULE, rule.id, "enabled", rule.enabled, lamport=lam, dev=dev
        )
    return rule


def delete_rule(conn: sqlite3.Connection, rule_id: str, *, track: bool = True) -> None:
    """Borrado lógico: un tombstone es lo único que se puede sincronizar."""
    lam = tick_lamport(conn)
    dev = device_id(conn)
    conn.execute(
        "UPDATE rules SET deleted = 1, lamport = ?, device_id = ?, updated_at = ? WHERE id = ?",
        (lam, dev, now_ms(), rule_id),
    )
    if track:
        repo.append_change(conn, Entity.RULE, rule_id, "deleted", True, lamport=lam, dev=dev)


# ------------------------------------------------------------------------ YAML
def import_rules_yaml(conn: sqlite3.Connection, text: str) -> int:
    """Carga un fichero de reglas. Valida TODO antes de escribir nada."""
    reglas = parse_rules(yaml.safe_load(text))
    for rule in reglas:
        save_rule(conn, rule)
    return len(reglas)


def export_rules_yaml(conn: sqlite3.Connection) -> str:
    reglas = load_rules(conn, include_disabled=True)
    datos = [r.to_yaml_obj() for r in reglas]
    return yaml.safe_dump(datos, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _row_to_rule(row: sqlite3.Row) -> Rule | None:
    """Una regla corrupta en la base no debe tumbar la carga de las demás."""
    try:
        rule = Rule.model_validate_json(row["spec_json"])
    except Exception:
        return None
    rule.id = row["id"]
    rule.name = row["name"]
    rule.enabled = bool(row["enabled"])
    rule.position = row["position"]
    return rule
