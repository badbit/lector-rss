"""Ejecución de las acciones de las reglas.

El motor decide *qué* hacer; este módulo lo hace. Las notificaciones no se envían
aquí: se devuelven en el `RuleOutcome` para que quien llama pueda agruparlas con
`notify.coalesce` y no soltar cuarenta avisos de golpe tras un refresco.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import repo
from ..models import Entry, ExportJob, ExportKind, Feed
from ..notify import Notification, Priority
from .engine import RuleEngine
from .models import Action, ActionKind

log = logging.getLogger("rsscore.rules")

__all__ = ["RuleOutcome", "apply_rules", "make_ingest_hook"]


@dataclass(slots=True)
class RuleOutcome:
    entry_id: str
    applied_rules: list[str] = field(default_factory=list)
    tags_added: list[str] = field(default_factory=list)
    tags_removed: list[str] = field(default_factory=list)
    starred: bool = False
    marked_read: bool = False
    notifications: list[Notification] = field(default_factory=list)
    exports_queued: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.applied_rules)


def apply_rules(
    conn: sqlite3.Connection,
    entry: Entry,
    feed: Feed,
    engine: RuleEngine,
    *,
    notifier: object | None = None,
) -> RuleOutcome:
    """Evalúa las reglas sobre una entrada y materializa sus acciones."""
    outcome = RuleOutcome(entry_id=entry.id)

    folder_names: list[str] = []
    if feed.folder_id:
        folder = repo.get_folder(conn, feed.folder_id)
        if folder:
            folder_names.append(folder.name)
    tag_names = [t.name for t in repo.entry_tags(conn, entry.id)]

    for rule, acciones in engine.matching_rules(
        entry, feed, folder_names=folder_names, tag_names=tag_names
    ):
        outcome.applied_rules.append(rule.name)
        for action in acciones:
            _run(conn, action, entry, feed, rule.name, outcome)
    return outcome


def _run(
    conn: sqlite3.Connection,
    action: Action,
    entry: Entry,
    feed: Feed,
    rule_name: str,
    outcome: RuleOutcome,
) -> None:
    match action.kind:
        case ActionKind.TAG:
            tag = repo.get_or_create_tag(conn, str(action.tag))
            repo.tag_entry(conn, entry.id, tag.id)
            outcome.tags_added.append(tag.name)

        case ActionKind.UNTAG:
            tag = repo.get_or_create_tag(conn, str(action.untag))
            repo.tag_entry(conn, entry.id, tag.id, remove=True)
            outcome.tags_removed.append(tag.name)

        case ActionKind.STAR:
            repo.set_starred(conn, [entry.id], bool(action.star))
            outcome.starred = bool(action.star)

        case ActionKind.MARK_READ:
            repo.set_read(conn, [entry.id], bool(action.mark_read))
            outcome.marked_read = bool(action.mark_read)

        case ActionKind.NOTIFY:
            spec = action.notify
            outcome.notifications.append(
                Notification(
                    title=(spec.title if spec and spec.title else rule_name),
                    body=entry.title,
                    priority=Priority(spec.priority) if spec else Priority.DEFAULT,
                    url=entry.url,
                    entry_id=entry.id,
                    tags=[feed.display_title],
                    rule_name=rule_name,
                )
            )

        case ActionKind.EXPORT:
            spec = action.export
            job = ExportJob(
                kind=ExportKind(spec.kind if spec else ExportKind.OBSIDIAN),
                target=(spec.target if spec else "desktop"),
                params={"entry_ids": [entry.id], "rule": rule_name},
            )
            repo.enqueue_export(conn, job)
            outcome.exports_queued.append(job.id)

        case ActionKind.STOP:  # lo resuelve el motor; aquí no llega
            pass


def make_ingest_hook(
    engine: RuleEngine,
    *,
    collector: list[Notification] | None = None,
) -> Callable[[sqlite3.Connection, Entry, Feed], None]:
    """Devuelve el callable que espera `Ingestor(on_new_entry=...)`.

    Captura sus propias excepciones a propósito: una regla mal escrita puede
    hacer cualquier cosa, pero nunca debe abortar la descarga de noticias.
    """

    def hook(conn: sqlite3.Connection, entry: Entry, feed: Feed) -> None:
        try:
            outcome = apply_rules(conn, entry, feed, engine)
            if collector is not None:
                collector.extend(outcome.notifications)
        except Exception:
            log.exception("Fallo aplicando reglas al artículo %s", entry.id)

    return hook
