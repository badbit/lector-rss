"""CLI/TUI comparten datos, caché y cambios sincronizables sin importar Qt."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from rsscore import cli, reader, repo
from rsscore.config import Config
from rsscore.db import open_db
from rsscore.models import Entry, Feed
from rsscore.tui import PAGE_SIZE, Library, TerminalReader, wrap_columns


@pytest.fixture
def library(tmp_path, monkeypatch):
    path = tmp_path / "rss.db"
    monkeypatch.setenv("RSS_DB", str(path))
    monkeypatch.setenv("RSS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("RSS_HUB_URL", raising=False)
    conn = open_db(path)
    feed = repo.add_feed(conn, Feed(url="https://example.org/feed", title="Ciencia"))
    entries = []
    for i in range(3):
        entry = Entry(feed_id=feed.id, guid_hash=str(i), content_hash=str(i),
                      title=f"Artículo {i}", published_at=1000 + i,
                      body_text=f"Contenido completo {i}", url=f"https://example.org/{i}")
        repo.insert_entry(conn, entry)
        entries.append(entry)
    yield conn, feed, entries
    conn.close()


def test_cli_json_filters_and_pagination(library, capsys):
    conn, feed, entries = library
    repo.set_read(conn, [entries[0].id])
    repo.set_starred(conn, [entries[1].id])
    assert cli.main(["entries", "--unread", "--starred", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [entries[1].id]
    assert rows[0]["state"]["starred"] is True
    assert cli.main(["entries", "--feed", feed.id, "-n", "1", "--offset", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == entries[1].id
    assert cli.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["unread"] == 2


def test_cli_show_and_state_changes_are_synchronized(library, capsys):
    conn, feed, entries = library
    entry = entries[0]
    assert cli.main(["show", entry.id, "--offline"]) == 0
    assert "Contenido completo 0" in capsys.readouterr().out
    assert not repo.get_state(conn, entry.id).read
    assert cli.main(["show", entry.id, "--mark-read", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "Contenido completo 0"
    assert repo.get_state(conn, entry.id).read
    assert any(op.entity_id == entry.id and op.field == "read" and op.value
               for _, op in repo.outbox_batch(conn))


def test_cli_search_prints_ids_and_json(library, capsys):
    conn, feed, entries = library
    assert cli.main(["search", '"completo 1"', "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == entries[1].id
    assert cli.main(["search", '"completo 1"']) == 0
    assert entries[1].id in capsys.readouterr().out


@pytest.mark.parametrize("args", [
    ["show", "missing"], ["search", '"'], ["read"], ["read", "--feed", "f", "--unread"],
])
def test_cli_expected_errors_are_concise(library, capsys, args):
    assert cli.main(args) == 1
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["entries", "-n", "0"], ["entries", "--offset", "-1"], ["unread", "-n", "-10"],
])
def test_cli_rejects_invalid_pagination(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2


def test_tui_rejects_pipe_without_creating_database(tmp_path, monkeypatch, capsys):
    path = tmp_path / "unused.db"
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["--db", str(path), "tui"]) == 1
    assert not path.exists()
    assert "terminal interactivo" in capsys.readouterr().err


def test_text_strips_html_and_terminal_controls():
    entry = Entry(feed_id="f", guid_hash="g", content_hash="h",
                  body_html='<p>Español &amp; 中文</p><script>danger()</script>\x1b[2J')
    text = reader.article_text(entry)
    assert "Español & 中文" in text
    assert "danger" not in text and "\x1b" not in text
    assert "\x07" not in reader.terminal_text("x\x07y")


def test_wrapping_wide_characters_does_not_lose_article_text():
    text = "中文" * 100
    lines = wrap_columns(text, 40)
    assert "".join(lines) == text
    assert all(len(line) <= 20 for line in lines)


@respx.mock
async def test_refresh_local_ingests_and_reports_download_errors(library):
    conn, feed, _ = library
    route = respx.get(feed.url).respond(200, text=(
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Ciencia</title>'
        '<link>https://example.org</link><description>Prueba</description>'
        '<item><guid>new</guid><title>Nuevo</title>'
        '<link>https://example.org/new</link></item></channel></rss>'
    ))
    assert "1 entradas nuevas" in await reader.refresh(conn, Config(), feed=feed)
    assert repo.search(conn, "Nuevo")
    route.respond(503)
    with pytest.raises(ValueError, match="con error"):
        await reader.refresh(conn, Config(), feed=repo.get_feed(conn, feed.id))


def test_cli_closes_database_after_command(library, monkeypatch, capsys):
    opened = []
    original = cli.open_db

    def record(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(cli, "open_db", record)
    assert cli.main(["entries", "--json"]) == 0
    capsys.readouterr()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@respx.mock
async def test_article_body_download_is_cached_and_offline_is_local(library):
    conn, feed, entries = library
    entry = Entry(feed_id=feed.id, guid_hash="remote", content_hash="remote", summary="Resumen")
    repo.insert_entry(conn, entry)
    cfg = Config(hub_url="https://hub.example", hub_token="test-token")
    route = respx.get(f"https://hub.example/entries/{entry.id}").mock(
        return_value=httpx.Response(200, json={"body_html": "<p>Completo</p>",
                                             "body_text": "Completo"})
    )
    offline = await reader.load_article(conn, cfg, entry.id, offline=True)
    assert reader.article_text(offline) == "Resumen"
    assert not route.called
    loaded = await reader.load_article(conn, cfg, entry.id)
    assert loaded.body_text == "Completo"
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"
    assert repo.get_body(conn, entry.id)[1] == "Completo"
    assert (await reader.load_article(conn, cfg, entry.id)).body_text == "Completo"
    assert route.call_count == 1


@respx.mock
async def test_empty_remote_body_can_be_retried(library):
    conn, feed, _ = library
    entry = Entry(feed_id=feed.id, guid_hash="empty", content_hash="empty")
    repo.insert_entry(conn, entry)
    route = respx.get(f"https://hub.example/entries/{entry.id}").respond(200, json={})
    cfg = Config(hub_url="https://hub.example")
    await reader.load_article(conn, cfg, entry.id)
    assert not repo.get_entry(conn, entry.id).has_body
    await reader.load_article(conn, cfg, entry.id)
    assert route.call_count == 2


@respx.mock
async def test_refresh_uses_hub_and_then_synchronizes(library, monkeypatch):
    conn, feed, _ = library
    sync = AsyncMock(return_value={})
    monkeypatch.setattr(reader, "sync", sync)
    route = respx.post("https://hub.example/feeds/refresh").respond(
        200, json={"feeds": 2, "nuevas": 5},
    )
    cfg = Config(hub_url="https://hub.example")
    assert "5 entradas nuevas" in await reader.refresh(conn, cfg, force=True)
    assert route.calls[0].request.url.params["force"] == "true"
    sync.assert_awaited_once_with(conn, cfg)


@respx.mock
async def test_subscribe_syncs_folder_before_creating_remote_feed(library, monkeypatch):
    conn, feed, _ = library
    events = []

    async def sync(*args):
        events.append("sync")

    def create(request):
        events.append("create")
        assert json.loads(request.content)["folder_id"] == "folder"
        return httpx.Response(200, json={"title": "Fuente remota"})

    monkeypatch.setattr(reader, "sync", sync)
    respx.post("https://hub.example/feeds").mock(side_effect=create)
    cfg = Config(hub_url="https://hub.example")
    assert await reader.subscribe(conn, cfg, feed.url, "folder") == "Fuente remota"
    assert events == ["sync", "create", "sync"]


def test_tui_pagination_filters_and_invalid_search_preserve_view(library):
    conn, feed, entries = library
    for i in range(PAGE_SIZE):
        repo.insert_entry(conn, Entry(feed_id=feed.id, guid_hash=f"extra{i}",
                                      content_hash=f"extra{i}", published_at=2000 + i))
    model = Library(conn)
    model.reload()
    assert len(model.entries) == PAGE_SIZE and model.has_more
    model.page(1)
    assert len(model.entries) == 3 and not model.has_more
    model.page(1)
    assert model.offset == PAGE_SIZE
    model.filter(query='"completo 1"')
    assert model.selected.id == entries[1].id and model.offset == 0
    with pytest.raises(sqlite3.Error):
        model.filter(query='"')
    assert model.query == '"completo 1"' and model.selected.id == entries[1].id
    model.toggle("starred")
    model.filter(starred=True)
    assert model.selected.id == entries[1].id
    assert repo.get_state(conn, entries[1].id).starred
    assert any(op.field == "starred" for _, op in repo.outbox_batch(conn))


class Screen:
    def __init__(self, size=(24, 80)):
        self.size, self.text = size, []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.text = []

    def addstr(self, y, x, value, attr):
        assert 0 <= y < self.size[0] and 0 <= x < self.size[1]
        self.text.append(value)

    def refresh(self):
        pass


def test_tui_keyboard_read_star_return_and_resize(library):
    conn, feed, entries = library
    screen = Screen()
    ui = TerminalReader(screen, conn, Config())
    ui.draw()
    assert any("Artículo" in line for line in screen.text)
    ui.handle("\n")
    assert ui.article.id == entries[-1].id
    assert repo.get_state(conn, ui.article.id).read
    ui.handle("s")
    assert repo.get_state(conn, ui.article.id).starred
    ui.handle("q")
    assert ui.article is None
    ui.handle("\t")
    ui.handle("j")
    assert ui.library.feed_id == feed.id
    screen.size = (4, 20)
    ui.draw()
    assert screen.text
    assert not ui.handle("q")


@respx.mock
def test_tui_hub_failure_still_opens_summary(library):
    conn, feed, _ = library
    entry = Entry(feed_id=feed.id, guid_hash="offline", content_hash="offline",
                  summary="Resumen local")
    repo.insert_entry(conn, entry)
    respx.get(f"https://hub.example/entries/{entry.id}").respond(503)
    ui = TerminalReader(Screen(), conn, Config(hub_url="https://hub.example"))
    ui.handle("\n")
    assert reader.article_text(ui.article) == "Resumen local"
    assert "Hub no disponible" in ui.message
