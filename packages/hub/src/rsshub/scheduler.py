"""Tareas programadas del hub: refresco de feeds, compactación y revistas."""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from rsscore import repo
from rsscore.config import Config

from .deps import bus, db, write_tx

log = logging.getLogger("rsshub.scheduler")


async def refresh_tick(cfg: Config) -> None:
    """Refresca los feeds que tocan. Cada feed lleva su propio intervalo y
    backoff, así que aquí solo hay que despertar a menudo y dejar que el
    planificador de `due_feeds` decida."""
    from rsscore.ingest import Ingestor

    conn = db()
    try:
        hook = _build_rules_hook(conn, cfg)
        results = await Ingestor(conn, cfg, on_new_entry=hook).refresh_due()
    except Exception:
        log.exception("Fallo en el ciclo de refresco")
        return
    nuevas = sum(len(r.new_entries) for r in results)
    if nuevas:
        log.info("Refresco: %d feeds, %d entradas nuevas", len(results), nuevas)
        bus.publish({"type": "entries_changed", "nuevas": nuevas})


def _build_rules_hook(conn, cfg: Config):
    """Engancha el motor de reglas a la ingesta, si hay reglas activas."""
    try:
        from rsscore.notify import build_notifier
        from rsscore.rules.apply import apply_rules
        from rsscore.rules.engine import RuleEngine
        from rsscore.rules.store import load_rules
    except ImportError:
        return None

    rules = load_rules(conn)
    if not rules:
        return None
    engine = RuleEngine(rules)
    notifier = build_notifier(cfg.notify)

    def hook(c, entry, feed) -> None:
        try:
            outcome = apply_rules(c, entry, feed, engine, notifier=notifier)
            for n in getattr(outcome, "notifications", []):
                bus.publish({"type": "alert", "title": n.title, "entry_id": entry.id})
        except Exception:
            log.exception("Fallo aplicando reglas a %s", entry.id)

    return hook


async def compact_tick(cfg: Config) -> None:
    """El diario de cambios crece sin límite en un archivo permanente."""
    from rsscore.sync import compact_change_log
    from rsscore.sync.compact import min_client_seq

    try:
        with write_tx() as c:
            keep = min_client_seq(c)
            borradas = compact_change_log(c, keep_seq=keep)
        if borradas:
            log.info("Compactación: %d operaciones colapsadas (keep_seq=%d)", borradas, keep)
    except Exception:
        log.exception("Fallo compactando el diario de cambios")


async def scheduled_magazines(cfg: Config) -> None:
    """Revistas programadas: se definen como trabajos de exportación con hora."""
    conn = db()
    pendientes = repo.list_exports(conn, 200)
    log.debug("Revistas programadas revisadas (%d trabajos recientes)", len(pendientes))


def build_scheduler(cfg: Config) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=datetime.now().astimezone().tzinfo)
    sched.add_job(
        refresh_tick,
        IntervalTrigger(minutes=1),
        args=[cfg],
        id="refresh",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        compact_tick,
        CronTrigger(hour=cfg.hub.compact_at_hour, minute=17),
        args=[cfg],
        id="compact",
        max_instances=1,
    )
    return sched
