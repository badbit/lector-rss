"""Pruebas de las notificaciones."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr
from rsscore.config import NotifyConfig
from rsscore.notify import (
    CompositeNotifier,
    Notification,
    NtfyNotifier,
    Priority,
    build_notifier,
    coalesce,
)


def aviso(titulo: str, cuerpo: str = "", regla: str | None = None) -> Notification:
    return Notification(title=titulo, body=cuerpo, rule_name=regla)


# ------------------------------------------------------------------ agrupación
def test_cuarenta_avisos_de_una_regla_se_agrupan_en_uno():
    """Un refresco puede disparar una regla cuarenta veces. Mandar cuarenta
    notificaciones haría el sistema inservible."""
    avisos = [aviso("Alertas Rust", f"Artículo {i}", "Alertas Rust") for i in range(40)]
    agrupados = coalesce(avisos)

    assert len(agrupados) == 1
    assert agrupados[0].count == 40
    assert "40" in agrupados[0].body


def test_reglas_distintas_no_se_mezclan():
    avisos = [
        aviso("Rust", "a", "Alertas Rust"),
        aviso("Rust", "b", "Alertas Rust"),
        aviso("Seguridad", "c", "Vulnerabilidades"),
    ]
    agrupados = coalesce(avisos)
    assert len(agrupados) == 2
    assert {n.count for n in agrupados} == {2, 1}


def test_un_solo_aviso_se_deja_intacto():
    original = aviso("Alertas Rust", "Rust 1.90", "Alertas Rust")
    agrupados = coalesce([original])
    assert len(agrupados) == 1
    assert agrupados[0].body == "Rust 1.90", "con un solo artículo hay que decir cuál es"


# ------------------------------------------------------------------------ ntfy
@respx.mock
async def test_ntfy_envia_las_cabeceras_esperadas():
    ruta = respx.post("https://ntfy.local/rss").mock(return_value=httpx.Response(200))
    notificador = NtfyNotifier(
        NotifyConfig(ntfy_url="https://ntfy.local/rss", ntfy_token=SecretStr("secreto"))
    )

    ok = await notificador.send(
        Notification(
            title="Vulnerabilidad grave",
            body="CVE-2026-1234",
            priority=Priority.HIGH,
            url="https://ejemplo.org/cve",
            tags=["seguridad"],
        )
    )

    assert ok is True
    peticion = ruta.calls[0].request
    assert peticion.headers["Authorization"] == "Bearer secreto"
    assert "Vulnerabilidad" in peticion.headers["Title"]
    assert peticion.headers["Priority"] in ("high", "4")
    assert peticion.headers["Click"] == "https://ejemplo.org/cve"


@respx.mock
async def test_ntfy_caido_no_lanza():
    respx.post("https://ntfy.local/rss").mock(side_effect=httpx.ConnectError("sin red"))
    notificador = NtfyNotifier(NotifyConfig(ntfy_url="https://ntfy.local/rss"))
    assert await notificador.send(aviso("x")) is False


def test_sin_url_configurada_no_esta_activo():
    assert NtfyNotifier(NotifyConfig(ntfy_url="")).enabled is False


# ------------------------------------------------------------------ compuesto
@respx.mock
async def test_un_canal_caido_no_impide_que_el_otro_entregue():
    respx.post("https://roto.local/x").mock(side_effect=httpx.ConnectError("no"))
    respx.post("https://bueno.local/x").mock(return_value=httpx.Response(200))

    compuesto = CompositeNotifier(
        [
            NtfyNotifier(NotifyConfig(ntfy_url="https://roto.local/x")),
            NtfyNotifier(NotifyConfig(ntfy_url="https://bueno.local/x")),
        ]
    )
    entregados = await compuesto.send(aviso("prueba"))
    assert entregados == 1


def test_la_fabrica_respeta_la_configuracion():
    cfg = NotifyConfig(
        ntfy_url="https://ntfy.local/rss", ntfy_token=SecretStr("t"), desktop_enabled=False
    )
    compuesto = build_notifier(cfg)
    assert isinstance(compuesto, CompositeNotifier)

    vacio = build_notifier(NotifyConfig(ntfy_url="", desktop_enabled=False))
    assert isinstance(vacio, CompositeNotifier)


@pytest.mark.parametrize("prioridad", list(Priority))
def test_todas_las_prioridades_se_traducen(prioridad):
    cabeceras = NtfyNotifier(NotifyConfig(ntfy_url="https://x.local/y")).headers(
        Notification(title="t", priority=prioridad)
    )
    assert cabeceras["Priority"]
