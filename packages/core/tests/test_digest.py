"""Reglas como filtros de revistas y extractos sin efectos sobre el archivo."""

from __future__ import annotations

import zipfile

import pytest
from rsscore import repo
from rsscore.cli import main
from rsscore.config import MagazineConfig
from rsscore.db import open_db
from rsscore.export.magazine import build_magazine
from rsscore.models import Entry, EntrySelection, Feed, Folder
from rsscore.rules import apply_rules
from rsscore.rules.engine import RuleEngine
from rsscore.rules.models import Rule
from rsscore.rules.selection import select_by_rules
from rsscore.rules.store import save_rule


@pytest.fixture
def archive(tmp_path):
    conn = open_db(tmp_path / "rss.db")
    parent = repo.upsert_folder(conn, Folder(name="Ciencia"))
    child = repo.upsert_folder(conn, Folder(name="Energía", parent_id=parent.id))
    feed = repo.add_feed(conn, Feed(url="https://example.test/rss", folder_id=child.id))
    for i in range(6):
        repo.insert_entry(conn, Entry(
            id=f"E{i}", feed_id=feed.id, guid_hash=str(i), content_hash=str(i),
            title=f"Noticia {i}", published_at=1000 - i,
            body_text=("solar " if i >= 3 else "deportes ") + "palabra " * 100,
            url=f"https://example.test/{i}",
        ))
    rule = Rule.model_validate({
        "name": "Solar", "scope": {"folders": [parent.id]},
        "when": {"all": [{"field": "content", "op": "contains", "value": "solar"}]},
        "then": [{"mark_read": True}, {"export": "kindle"}],
    })
    save_rule(conn, rule)
    yield conn, feed, rule, tmp_path
    conn.close()


def test_limite_se_aplica_despues_de_filtrar_sin_ejecutar_acciones(archive):
    conn, _, rule, _ = archive
    before = conn.total_changes
    entries = select_by_rules(conn, EntrySelection(limit=2), [rule])
    assert [e.id for e in entries] == ["E3", "E4"]
    assert conn.total_changes == before
    assert repo.list_exports(conn) == []
    assert not repo.get_state(conn, "E3").read


def test_regla_sin_acciones_tambien_sirve_como_filtro(archive):
    conn, _, rule, _ = archive
    rule.then = []
    assert len(select_by_rules(conn, EntrySelection(limit=10), [rule])) == 3


def test_filtrado_busca_mas_alla_de_primera_pagina(archive):
    conn, feed, rule, _ = archive
    for i in range(505):
        repo.insert_entry(conn, Entry(
            id=f"extra-{i}", feed_id=feed.id, guid_hash=f"extra-{i}",
            content_hash=f"extra-{i}", title="Sin coincidencia", published_at=2000 + i,
        ))
    entries = select_by_rules(conn, EntrySelection(limit=2), [rule])
    assert [entry.id for entry in entries] == ["E3", "E4"]


def test_union_de_reglas_no_duplica_ni_obedece_stop(archive):
    conn, _, solar, _ = archive
    first = Rule.model_validate({
        "name": "Todas", "when": {}, "then": [{"stop": True}],
    })
    entries = select_by_rules(conn, EntrySelection(limit=10), [first, solar])
    assert {entry.id for entry in entries} == {f"E{i}" for i in range(6)}
    assert len(entries) == 6


def test_carpeta_vacia_no_se_convierte_en_todo_el_archivo(archive):
    conn, _, _, _ = archive
    folder = repo.upsert_folder(conn, Folder(name="Vacía"))
    assert repo.select_entries(conn, EntrySelection(folder_ids=[folder.id])) == []


def test_varias_etiquetas_no_duplican_articulos(archive):
    conn, _, _, _ = archive
    tags = [repo.get_or_create_tag(conn, name) for name in ("a", "b")]
    for tag in tags:
        repo.tag_entry(conn, "E0", tag.id)
    selected = repo.select_entries(conn, EntrySelection(tag_ids=[t.id for t in tags]))
    assert [e.id for e in selected] == ["E0"]


def test_acciones_aplican_ambito_de_carpetas_ascendientes(archive):
    conn, feed, rule, _ = archive
    outcome = apply_rules(conn, repo.get_entry(conn, "E3"), feed, RuleEngine([rule]))
    assert outcome.marked_read


@pytest.mark.parametrize("mode", ["full", "excerpt"])
def test_epub_incluye_texto_plano_o_extracto_y_fuente(archive, mode):
    conn, _, rule, tmp = archive
    result = build_magazine(conn, EntrySelection(limit=2), MagazineConfig(
        rules=[rule.name], content_mode=mode, excerpt_words=30, embed_images=False,
    ), out_path=tmp / f"{mode}.epub")
    assert result.articles == 2
    with zipfile.ZipFile(result.path) as book:
        text = book.read("EPUB/art_0001.xhtml").decode()
    assert "solar" in text and "https://example.test/" in text
    if mode == "excerpt":
        assert "Extracto del artículo" in text
        assert text.count("palabra") == 29
    else:
        assert text.count("palabra") == 100


def test_regla_inexistente_no_exporta_todo_por_error(archive):
    conn, _, _, tmp = archive
    with pytest.raises(ValueError, match="inexistente"):
        build_magazine(conn, EntrySelection(), MagazineConfig(rules=["no-existe"]), out_path=tmp)
    assert not list(tmp.glob("*.epub"))


def test_regla_desactivada_no_amplia_la_seleccion(archive):
    conn, _, rule, tmp = archive
    rule.enabled = False
    save_rule(conn, rule)
    with pytest.raises(ValueError, match="desactivada"):
        build_magazine(conn, EntrySelection(), MagazineConfig(rules=[rule.id]), out_path=tmp)
    assert not list(tmp.glob("*.epub"))


def test_extracto_prefiere_resumen_del_feed_sin_modificar_original(archive):
    conn, feed, _, tmp = archive
    repo.insert_entry(conn, Entry(
        id="resumen", feed_id=feed.id, guid_hash="resumen", content_hash="resumen",
        title="Resumen editorial", summary="<p>Texto <b>editorial</b> &amp; fuente</p>",
        body_text="Cuerpo completo distinto", published_at=9000,
    ))
    result = build_magazine(conn, EntrySelection(limit=1), MagazineConfig(
        content_mode="excerpt", embed_images=False,
    ), out_path=tmp)
    with zipfile.ZipFile(result.path) as book:
        article = book.read("EPUB/art_0001.xhtml").decode()
    assert "Extracto del feed" in article and "editorial &amp; fuente" in article
    assert "Cuerpo completo distinto" not in article
    assert repo.get_entry(conn, "resumen", with_body=True).body_text == "Cuerpo completo distinto"


def test_generar_dos_revistas_en_directorio_no_sobrescribe(archive):
    conn, _, _, tmp = archive
    cfg = MagazineConfig(output_dir=tmp, embed_images=False)
    first = build_magazine(conn, EntrySelection(limit=1), cfg)
    original = first.path.read_bytes()
    second = build_magazine(conn, EntrySelection(limit=2), cfg)
    assert first.path != second.path
    assert first.path.read_bytes() == original
    assert zipfile.is_zipfile(second.path)


def test_cli_preview_no_crea_libro_ni_envia_correo(archive, capsys):
    _, _, rule, tmp = archive
    result = main([
        "--db", str(tmp / "rss.db"), "digest", "--rule", rule.name,
        "--limit", "2", "--brief", "--preview", "--send-to-kindle", "--out", str(tmp),
    ])
    assert result == 0
    assert "2 artículos seleccionados" in capsys.readouterr().out
    assert not list(tmp.glob("*.epub"))


def test_cli_genera_extractos_con_regla(archive, capsys):
    _, _, rule, tmp = archive
    out = tmp / "cli.epub"
    assert main([
        "--db", str(tmp / "rss.db"), "digest", "--rule", rule.id,
        "--brief", "--words", "30", "--out", str(out),
    ]) == 0
    assert "3 artículos" in capsys.readouterr().out
    with zipfile.ZipFile(out) as book:
        assert "Extracto del artículo" in book.read("EPUB/art_0001.xhtml").decode()


def test_cli_fallo_correo_conserva_epub_y_devuelve_error(archive, capsys, monkeypatch):
    from rsscore.export.kindle import KindleError

    async def fail(*args, **kwargs):
        raise KindleError("Falta configurar SMTP")

    monkeypatch.setattr("rsscore.export.kindle.send_epub_file", fail)
    _, _, rule, tmp = archive
    out = tmp / "conservado.epub"
    assert main([
        "--db", str(tmp / "rss.db"), "digest", "--rule", rule.id,
        "--brief", "--out", str(out), "--send-to-kindle",
    ]) == 1
    assert "Falta configurar SMTP" in capsys.readouterr().err
    assert zipfile.is_zipfile(out)
