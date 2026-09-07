"""Selección por condiciones de reglas, sin alterar marcas ni encolar envíos."""

from __future__ import annotations

import sqlite3

from .. import repo
from ..models import Entry, EntrySelection, Feed
from .engine import RuleEngine
from .models import Rule
from .store import load_rules


def rule_context(conn: sqlite3.Connection, entry: Entry, feed: Feed) -> dict:
    folders: list[str] = []
    seen: set[str] = set()
    current = feed.folder_id
    while current and current not in seen:
        seen.add(current)
        folder = repo.get_folder(conn, current)
        if folder is None:
            break
        folders.extend([folder.id, folder.name])
        current = folder.parent_id
    tags = [token for t in repo.entry_tags(conn, entry.id) for token in (t.id, t.name)]
    return {"folder_names": folders, "tag_names": tags}


def resolve_rules(conn: sqlite3.Connection, references: list[str]) -> list[Rule]:
    available = load_rules(conn, include_disabled=True)
    selected: dict[str, Rule] = {}
    for ref in references:
        found = [r for r in available if r.id == ref] or [r for r in available if r.name == ref]
        if len(found) != 1:
            raise ValueError(f"Regla inexistente o ambigua: {ref}; usa su id")
        rule = found[0]
        if not rule.enabled:
            raise ValueError(f"La regla está desactivada: {rule.name}")
        selected[rule.id] = rule
    return list(selected.values())


def select_by_rules(
    conn: sqlite3.Connection, selection: EntrySelection, rules: list[Rule],
) -> list[Entry]:
    """El límite cuenta coincidencias, no candidatos anteriores al filtrado."""
    if selection.limit <= 0 or selection.offset < 0:
        raise ValueError("El límite debe ser positivo y el desplazamiento no negativo")
    engine = RuleEngine(rules)
    result: list[Entry] = []
    page = selection.model_copy(update={"limit": 500})
    while len(result) < selection.limit:
        candidates = repo.select_entries(conn, page)
        for entry in repo.iter_entries_with_body(conn, candidates):
            feed = repo.get_feed(conn, entry.feed_id)
            if feed and engine.matching_conditions(entry, feed, **rule_context(conn, entry, feed)):
                result.append(entry)
                if len(result) == selection.limit:
                    break
        if len(candidates) < page.limit:
            break
        page.offset += len(candidates)
    return result
