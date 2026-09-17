"""Operaciones de lectura para clientes de terminal, sin dependencias gráficas."""

from __future__ import annotations

import sqlite3
import unicodedata

import httpx
from bs4 import BeautifulSoup

from . import repo
from .config import Config
from .models import Entry
from .sync import SyncClient, SyncStats


def terminal_text(text: str) -> str:
    """Impide que contenido remoto emita controles o escapes en el terminal."""
    return "".join(
        ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    ).expandtabs(4)


def plain_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return terminal_text(soup.get_text("\n", strip=True))


def article_text(entry: Entry) -> str:
    return terminal_text(entry.body_text) if entry.body_text else plain_html(
        entry.body_html or entry.summary or "Sin contenido almacenado."
    )


def hub_client(cfg: Config) -> httpx.AsyncClient:
    token = cfg.hub_token.get_secret_value()
    return httpx.AsyncClient(
        base_url=cfg.hub_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=cfg.fetch.timeout_seconds,
    )


async def sync(conn: sqlite3.Connection, cfg: Config) -> SyncStats:
    if not cfg.hub_url:
        raise ValueError("Falta hub_url en la configuración")
    async with SyncClient(conn, cfg.hub_url, cfg.hub_token.get_secret_value()) as client:
        return await client.sync_once(name=cfg.device_name or "terminal")


async def load_article(
    conn: sqlite3.Connection, cfg: Config, entry_id: str, *, offline: bool = False
) -> Entry:
    entry = repo.get_entry(conn, entry_id)
    if entry is None:
        raise ValueError(f"No encuentro el artículo: {entry_id}")
    if not offline and cfg.hub_url and not (entry.body_html or entry.body_text):
        async with hub_client(cfg) as client:
            response = await client.get(f"/entries/{entry_id}")
            response.raise_for_status()
            data = response.json()
        html, text = data.get("body_html"), data.get("body_text")
        if html or text:
            repo.update_entry_body(conn, entry_id, html=html, text=text)
            entry.body_html, entry.body_text = html, text
            entry.has_body = True
    return entry


async def refresh(conn: sqlite3.Connection, cfg: Config, *, feed=None, force=False) -> str:
    if not cfg.desktop_fetches_locally:
        async with hub_client(cfg) as client:
            route = f"/feeds/{feed.id}/refresh" if feed else "/feeds/refresh"
            response = await client.post(route, params={"force": force} if not feed else None)
            response.raise_for_status()
            data = response.json()
        await sync(conn, cfg)
        return f"{data.get('feeds', 1)} feeds refrescados, {data.get('nuevas', 0)} entradas nuevas"

    from .ingest import Ingestor
    from .rules import RuleEngine, load_rules, make_ingest_hook

    rules = load_rules(conn)
    hook = make_ingest_hook(RuleEngine(rules)) if rules else None
    async with Ingestor(conn, cfg, on_new_entry=hook) as ingestor:
        if feed:
            results = [await ingestor.refresh_feed(feed)]
        elif force:
            results = [await ingestor.refresh_feed(f) for f in repo.list_feeds(conn)]
        else:
            results = await ingestor.refresh_due()
    errors = [r for r in results if r.status == "error"]
    message = (
        f"{len(results)} feeds refrescados, "
        f"{sum(len(r.new_entries) for r in results)} entradas nuevas"
    )
    if errors:
        raise ValueError(f"{message}; {len(errors)} con error: {errors[0].error}")
    return message


async def subscribe(conn: sqlite3.Connection, cfg: Config, url: str, folder_id=None) -> str:
    if cfg.desktop_fetches_locally:
        from .ingest import Ingestor

        async with Ingestor(conn, cfg) as ingestor:
            feed = await ingestor.add_by_url(url, folder_id=folder_id)
        return f"{feed.display_title}  [{feed.id}]"
    # La carpeta recién creada debe existir en el hub antes de asignarla al feed.
    await sync(conn, cfg)
    async with hub_client(cfg) as client:
        response = await client.post("/feeds", json={"url": url, "folder_id": folder_id})
        response.raise_for_status()
        data = response.json()
    await sync(conn, cfg)
    return str(data.get("title") or url)
