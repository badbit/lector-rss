"""Pruebas del motor de reglas."""

from __future__ import annotations

import time

import pytest
from rsscore import repo
from rsscore.db import open_db
from rsscore.ids import hash_content, hash_guid
from rsscore.models import Entry, Feed, Folder
from rsscore.rules import (
    RuleEngine,
    apply_rules,
    export_rules_yaml,
    fold,
    import_rules_yaml,
    load_rules,
    parse_rules,
)

YAML_BASE = r"""
- name: Silenciar patrocinados
  when: { all: [{ field: title, op: contains, value: patrocinado }] }
  then: [{ mark_read: true }, { stop: true }]

- name: Alertas Rust
  when:
    any:
      - { field: title,   op: matches,  value: '(?i)\brust\b' }
      - { field: content, op: contains, value: cargo }
  scope: { folders: [Dev] }
  then: [{ tag: rust }, { star: true }, { notify: { priority: high } }]
"""


@pytest.fixture
def entorno(tmp_path):
    conn = open_db(tmp_path / "reglas.db", device_name="test")
    dev = repo.upsert_folder(conn, Folder(name="Dev"))
    cocina = repo.upsert_folder(conn, Folder(name="Cocina"))
    feed_dev = repo.add_feed(conn, Feed(url="https://a.org/f", title="Blog", folder_id=dev.id))
    feed_otro = repo.add_feed(
        conn, Feed(url="https://b.org/f", title="Recetas", folder_id=cocina.id)
    )
    return conn, feed_dev, feed_otro


def crear(conn, feed, titulo, cuerpo="", autor=None):
    entrada = Entry(
        feed_id=feed.id,
        guid_hash=hash_guid(feed.id, titulo),
        content_hash=hash_content(titulo),
        title=titulo,
        body_text=cuerpo,
        author=autor,
    )
    repo.insert_entry(conn, entrada)
    return entrada


# ------------------------------------------------------------------ básicos
def test_condiciones_anidadas(entorno):
    conn, feed, _ = entorno
    reglas = parse_rules(
        [
            {
                "name": "anidada",
                "when": {
                    "all": [
                        {"field": "title", "op": "contains", "value": "python"},
                        {
                            "any": [
                                {"field": "content", "op": "contains", "value": "asyncio"},
                                {"field": "content", "op": "contains", "value": "trio"},
                            ]
                        },
                    ],
                    "none": [{"field": "title", "op": "contains", "value": "obsoleto"}],
                },
                "then": [{"tag": "py"}],
            }
        ]
    )
    engine = RuleEngine(reglas)
    casos = [
        ("Python y asyncio", "usando asyncio", True),
        ("Python a secas", "sin nada", False),
        ("Python obsoleto con trio", "trio", False),
        ("Rust y asyncio", "asyncio", False),
    ]
    for titulo, cuerpo, esperado in casos:
        entrada = crear(conn, feed, titulo, cuerpo)
        acciones = engine.evaluate(entrada, feed, folder_names=["Dev"])
        assert bool(acciones) is esperado, titulo


def test_ignora_mayusculas_y_acentos(entorno):
    conn, feed, _ = entorno
    engine = RuleEngine(
        parse_rules(
            [{"name": "e", "when": {"all": [{"field": "title", "op": "contains",
                                             "value": "energia"}]}, "then": [{"tag": "e"}]}]
        )
    )
    for titulo in ("La ENERGÍA solar", "energia eólica", "Energia"):
        assert engine.evaluate(crear(conn, feed, titulo), feed), titulo
    assert fold("Energía") == "energia"


def test_regex_invalida_se_rechaza_al_validar():
    with pytest.raises(Exception) as exc:
        parse_rules(
            [{"name": "mala", "when": {"all": [{"field": "title", "op": "matches",
                                                "value": "(sin cerrar"}]}, "then": [{"tag": "x"}]}]
        )
    assert "regular" in str(exc.value).lower() or "regex" in str(exc.value).lower()


def test_ambito_por_carpeta(entorno):
    conn, feed_dev, feed_otro = entorno
    engine = RuleEngine(parse_rules(__import__("yaml").safe_load(YAML_BASE)))

    dentro = crear(conn, feed_dev, "Novedades de Rust")
    fuera = crear(conn, feed_otro, "Rust en la cocina")

    assert engine.evaluate(dentro, feed_dev, folder_names=["Dev"])
    assert not engine.evaluate(fuera, feed_otro, folder_names=["Cocina"])


def test_stop_corta_las_reglas_siguientes(entorno):
    conn, feed, _ = entorno
    import yaml

    engine = RuleEngine(parse_rules(yaml.safe_load(YAML_BASE)))
    entrada = crear(conn, feed, "Contenido patrocinado sobre Rust")
    outcome = apply_rules(conn, entrada, feed, engine)

    assert outcome.marked_read is True
    assert outcome.starred is False, "`stop` debe impedir que actúen las reglas de abajo"
    assert repo.entry_tags(conn, entrada.id) == []


def test_las_acciones_se_materializan_en_la_base(entorno):
    conn, feed, _ = entorno
    import yaml

    engine = RuleEngine(parse_rules(yaml.safe_load(YAML_BASE)))
    entrada = crear(conn, feed, "Rust 1.90 disponible")
    outcome = apply_rules(conn, entrada, feed, engine)

    assert [t.name for t in repo.entry_tags(conn, entrada.id)] == ["rust"]
    assert repo.get_state(conn, entrada.id).starred is True
    assert len(outcome.notifications) == 1
    assert outcome.notifications[0].priority == "high"


def test_accion_de_exportacion_encola_trabajo(entorno):
    conn, feed, _ = entorno
    engine = RuleEngine(
        parse_rules(
            [{"name": "exportar",
              "when": {"all": [{"field": "title", "op": "contains", "value": "guardar"}]},
              "then": [{"export": {"kind": "obsidian", "target": "desktop"}}]}]
        )
    )
    entrada = crear(conn, feed, "Esto hay que guardar")
    outcome = apply_rules(conn, entrada, feed, engine)

    assert len(outcome.exports_queued) == 1
    trabajo = repo.claim_export(conn, "desktop")
    assert trabajo is not None
    assert trabajo.params["entry_ids"] == [entrada.id]


# ----------------------------------------------------------------- YAML
def test_ida_y_vuelta_yaml(entorno):
    conn, _, _ = entorno
    assert import_rules_yaml(conn, YAML_BASE) == 2

    reglas = load_rules(conn)
    assert [r.name for r in reglas] == ["Silenciar patrocinados", "Alertas Rust"]

    exportado = export_rules_yaml(conn)
    conn2 = open_db(":memory:")
    assert import_rules_yaml(conn2, exportado) == 2
    assert [r.name for r in load_rules(conn2)] == [r.name for r in reglas]


def test_las_reglas_se_sincronizan(entorno):
    """Crear una regla debe generar operaciones en el diario de cambios."""
    conn, _, _ = entorno
    import_rules_yaml(conn, YAML_BASE)
    ops, _, _ = repo.changes_since(conn, 0, 1000)
    assert any(op.entity == "rule" for op in ops)


# ----------------------------------------------------------- rendimiento
def test_diez_mil_entradas_contra_cincuenta_reglas(entorno):
    """Las regex se precompilan una vez; recompilarlas por artículo sería el
    error clásico de rendimiento aquí."""
    conn, feed, _ = entorno
    reglas = parse_rules(
        [
            {
                "name": f"regla-{i}",
                "when": {"any": [
                    {"field": "title", "op": "matches", "value": rf"(?i)\bpalabra{i}\b"},
                    {"field": "content", "op": "contains", "value": f"termino{i}"},
                ]},
                "then": [{"tag": f"t{i}"}],
            }
            for i in range(50)
        ]
    )
    engine = RuleEngine(reglas)
    entradas = [
        Entry(feed_id=feed.id, guid_hash=f"g{i}", content_hash=f"c{i}",
              title=f"Titular número {i} sobre cosas", body_text="cuerpo de prueba " * 20)
        for i in range(10_000)
    ]
    inicio = time.perf_counter()
    for entrada in entradas:
        engine.evaluate(entrada, feed)
    transcurrido = time.perf_counter() - inicio
    # Tarda ~3 s en un portátil normal. El margen es amplio a propósito para que
    # el test detecte una regresión de verdad (como recompilar o volver a
    # normalizar por condición, que costaba 16 s) sin ser frágil en CI.
    assert transcurrido < 8.0, f"demasiado lento: {transcurrido:.2f}s para 10k x 50 reglas"
