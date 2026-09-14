"""Importaciones repetibles, conservadoras y atómicas con ZIP sintéticos."""

import json
import sqlite3
import zipfile

import pytest
from rsscore import cli, repo
from rsscore.db import open_db
from rsscore.inoreader import import_archive, read_archive
from rsscore.models import Entry, EntrySelection
from rsscore.rules.smart import list_saved_searches, run_saved_search
from rsscore.sync import apply_ops

XML = b'''<opml version="1.0"><body>
<outline text="Primera"><outline text="Ejemplo" xmlUrl="https://example.org/feed"/></outline>
<outline text="Segunda"><outline text="Ejemplo" xmlUrl="https://example.org/feed"/></outline>
</body></opml>'''


def write_zip(tmp_path, *, xml=XML, items=None, extra=None):
    if items is None:
        items = [{
            "id": f"inoreader-{i}", "title": f"Artículo {i}", "published": 1700000000 + i,
            "starred": 1700001000 + i, "crawlTimeMsec": "1700002000000",
            "origin": {"streamId": "feed/https://example.org/feed"},
            "canonical": [{"href": f"https://example.org/article/{i}"}],
            "categories": ["user/test/state/com.google/read", "user/test/label/Primera"],
            "summary": {"content": f'<p>Contenido {i}</p><script>alert(1)</script>'},
            "annotations": [{"note": "Dato conservado como procedencia"}],
        } for i in range(2)]
    path = tmp_path / "inoreader.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("subscriptions.xml", xml)
        archive.writestr("starred.json", json.dumps({"id": "user/test/starred", "items": items}))
        if extra:
            archive.writestr(extra, "dato")
    return path


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "rss.db")
    yield c
    c.close()


def test_import_preserves_content_dates_metadata_and_secondary_folder(tmp_path, conn):
    archive = read_archive(write_zip(tmp_path))
    report = import_archive(conn, archive)
    assert (report.feeds_new, report.folders_new, report.entries_new) == (1, 2, 2)
    assert report.views_created == 1
    entries = repo.select_entries(conn, EntrySelection(limit=10))
    assert len(entries) == 2
    for entry in entries:
        i = int(entry.title[-1])
        state = repo.get_state(conn, entry.id)
        assert state.read and state.starred
        assert state.star_at == (1700001000 + i) * 1000
        assert state.read_at is None
        html, text = repo.get_body(conn, entry.id)
        assert "Contenido" in text and "<script" not in html
        original = conn.execute("SELECT raw_json FROM import_records WHERE entry_id=?",
                                (entry.id,)).fetchone()[0]
        assert json.loads(original)["annotations"]
        assert "<script" in json.loads(original)["summary"]["content"]
    view = list_saved_searches(conn)[0]
    assert view.name == "Segunda · Inoreader"
    assert len(run_saved_search(conn, view)) == 2
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_reimport_keeps_user_changes_and_does_not_duplicate_journal(tmp_path, conn):
    archive = read_archive(write_zip(tmp_path))
    import_archive(conn, archive)
    entry = repo.select_entries(conn, EntrySelection())[0]
    repo.set_read(conn, [entry.id], False)
    repo.set_starred(conn, [entry.id], False)
    operations = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    report = import_archive(conn, archive)
    assert report.entries_skipped == 2 and report.entries_new == 0
    assert report.views_created == 0
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == operations
    state = repo.get_state(conn, entry.id)
    assert not state.read and not state.starred
    repo.delete_entry(conn, entry.id)
    import_archive(conn, archive)
    assert repo.get_entry(conn, entry.id) is None


def test_match_existing_url_preserves_body_and_unread_state(tmp_path, conn):
    archive = read_archive(write_zip(tmp_path))
    from rsscore.opml import import_opml

    import_opml(conn, archive.xml)
    feed = repo.list_feeds(conn)[0]
    entry = Entry(feed_id=feed.id, guid_hash="feed-guid", content_hash="feed-body",
                  url="https://example.org/article/0?utm_source=test", published_at=1700000000000,
                  body_text="Cuerpo existente más completo")
    repo.insert_entry(conn, entry)
    report = import_archive(conn, archive)
    assert (report.entries_new, report.entries_linked) == (1, 1)
    assert repo.get_entry(conn, entry.id).body_text == "Cuerpo existente más completo"
    assert not repo.get_state(conn, entry.id).read
    assert repo.get_state(conn, entry.id).starred


def test_import_rolls_back_everything_when_an_insert_fails(tmp_path, conn, monkeypatch):
    archive = read_archive(write_zip(tmp_path))
    original = repo.insert_entry
    calls = 0

    def insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.IntegrityError("fallo de prueba")
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "insert_entry", insert)
    with pytest.raises(sqlite3.IntegrityError):
        import_archive(conn, archive)
    for table in ("feeds", "folders", "entries", "import_records", "import_batches", "change_log"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_import_emits_metadata_and_original_star_date_for_clients(tmp_path, conn):
    import_archive(conn, read_archive(write_zip(tmp_path)))
    client = open_db(":memory:")
    try:
        ops, _, _ = repo.changes_since(conn, 0, 1000)
        result = apply_ops(client, ops)
        assert not result.errors and not result.pending
        entry = repo.select_entries(conn, EntrySelection())[0]
        remote = repo.get_entry(client, entry.id)
        assert remote and not remote.has_body  # se pide al hub al abrir el artículo
        assert repo.get_state(client, entry.id).star_at == repo.get_state(conn, entry.id).star_at
        assert list_saved_searches(client)[0].name == "Segunda · Inoreader"
    finally:
        client.close()


@pytest.mark.parametrize("extra", ["../danger", "new-export-format.json"])
def test_rejects_unsupported_members_instead_of_silently_losing_them(tmp_path, extra):
    with pytest.raises(ValueError):
        read_archive(write_zip(tmp_path, extra=extra))


def test_rejects_broken_opml_before_opening_destination(tmp_path):
    with pytest.raises(ValueError, match="OPML"):
        read_archive(write_zip(tmp_path, xml=b"<broken"))


def test_cli_preview_and_restorable_backup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RSS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("RSS_HUB_URL", raising=False)
    path = tmp_path / "destination.db"
    archive = write_zip(tmp_path)
    args = ["--db", str(path), "import-inoreader", str(archive), "--json"]
    assert cli.main(args) == 0
    assert not path.exists()
    assert json.loads(capsys.readouterr().out)["entries_new"] == 2
    c = open_db(path)
    c.close()
    before = path.read_bytes()
    assert cli.main(args) == 0
    capsys.readouterr()
    assert path.read_bytes() == before
    assert not (tmp_path / "backups").exists()
    assert cli.main([*args, "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    with sqlite3.connect(report["backup"]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2


def test_cli_hub_client_cannot_import_bodies_only_locally(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RSS_HUB_URL", "https://hub.example")
    path = tmp_path / "unused.db"
    assert cli.main(["--db", str(path), "import-inoreader", str(write_zip(tmp_path)),
                     "--apply"]) == 1
    assert not path.exists()
    assert "base del hub" in capsys.readouterr().err
