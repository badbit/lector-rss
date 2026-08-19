"""Trabajo en segundo plano: refresco, sincronización, exportaciones y alertas.

Todo corre en el bucle de asyncio que `qasync` comparte con Qt, así que se puede
tocar la interfaz directamente desde estas corrutinas sin marshalling entre
hilos. Lo que no se puede es bloquear: cualquier cosa lenta tiene que ser await.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable

from rsscore import repo
from rsscore.config import Config
from rsscore.models import ExportKind
from rsscore.notify import Notification, build_notifier, coalesce

log = logging.getLogger("rssdesk.tasks")


class Backend:
    """Envuelve el núcleo para que la ventana no sepa de asyncio ni de SQL."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self.conn = conn
        self.cfg = cfg
        self.notificador = build_notifier(cfg.notify)
        self._refrescando = False

    # ------------------------------------------------------------- refresco
    async def refrescar(
        self, *, todos: bool = False, feed_id: str | None = None
    ) -> tuple[int, int]:
        """Descarga novedades. Devuelve (feeds procesados, entradas nuevas)."""
        if self._refrescando:
            return (0, 0)
        self._refrescando = True
        avisos: list[Notification] = []
        try:
            from rsscore.ingest import Ingestor

            hook = self._hook_reglas(avisos)
            async with Ingestor(self.conn, self.cfg, on_new_entry=hook) as ing:
                if feed_id:
                    feed = repo.get_feed(self.conn, feed_id)
                    resultados = [await ing.refresh_feed(feed)] if feed else []
                elif todos:
                    resultados = await ing.refresh_all()
                else:
                    resultados = await ing.refresh_due()
        except Exception:
            log.exception("Fallo al refrescar")
            return (0, 0)
        finally:
            self._refrescando = False

        if avisos:
            await self._enviar_avisos(avisos)
        return (len(resultados), sum(len(r.new_entries) for r in resultados))

    def _hook_reglas(self, avisos: list[Notification]) -> Callable | None:
        try:
            from rsscore.rules import RuleEngine, load_rules, make_ingest_hook
        except ImportError:
            return None
        reglas = load_rules(self.conn)
        if not reglas:
            return None
        return make_ingest_hook(RuleEngine(reglas), collector=avisos)

    async def _enviar_avisos(self, avisos: list[Notification]) -> None:
        """Agrupadas: cuarenta artículos de una regla son UN aviso, no cuarenta."""
        for aviso in coalesce(avisos):
            try:
                await self.notificador.send(aviso)
            except Exception:
                log.exception("No se pudo enviar la notificación")

    # ------------------------------------------------------- sincronización
    async def sincronizar(self) -> str:
        if not self.cfg.hub_url:
            return "sin hub configurado"
        from rsscore.sync import SyncClient

        cliente = SyncClient(self.conn, self.cfg.hub_url, self.cfg.hub_token.get_secret_value())
        try:
            stats = await cliente.sync_once(name=self.cfg.device_name or "escritorio")
            return str(stats)
        except Exception as exc:
            log.warning("Sincronización fallida: %s", exc)
            return f"error: {exc}"
        finally:
            await cliente.aclose()

    # --------------------------------------------------------- exportación
    async def exportar_obsidian(self, entry_ids: list[str]) -> list[str]:
        from rsscore.export.obsidian import export_to_obsidian

        if not self.cfg.obsidian.vault_path:
            raise RuntimeError("Configura obsidian.vault_path en el config.yaml")
        rutas = await asyncio.to_thread(export_to_obsidian, self.conn, entry_ids, self.cfg.obsidian)
        return [str(r) for r in rutas]

    async def enviar_kindle(self, entry_ids: list[str]) -> str:
        from rsscore.export.kindle import send_to_kindle

        if not self.cfg.smtp.host or not self.cfg.smtp.kindle_address:
            raise RuntimeError("Configura el SMTP y la dirección @kindle.com")
        await send_to_kindle(self.conn, entry_ids, self.cfg.smtp)
        return f"{len(entry_ids)} artículos enviados al Kindle"

    async def generar_revista(self, seleccion) -> str:
        from rsscore.export.magazine import build_magazine

        resultado = await asyncio.to_thread(build_magazine, self.conn, seleccion, self.cfg.magazine)
        return str(getattr(resultado, "path", resultado))

    # ------------------------------------------------ cola de exportaciones
    async def worker_exportaciones(self, stop: asyncio.Event, *, target: str = "desktop") -> None:
        """Materializa lo que el móvil encoló: la bóveda de Obsidian está aquí.

        El hub no puede escribir en ella, así que el escritorio hace de brazo
        ejecutor cuando está encendido.
        """
        while not stop.is_set():
            try:
                trabajo = repo.claim_export(self.conn, target)
                if trabajo is None:
                    await asyncio.wait_for(stop.wait(), timeout=10)
                    continue
                await self._ejecutar_trabajo(trabajo)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fallo en la cola de exportaciones")
                await asyncio.sleep(10)

    async def _ejecutar_trabajo(self, trabajo) -> None:
        try:
            entry_ids = trabajo.params.get("entry_ids", [])
            if trabajo.kind == ExportKind.OBSIDIAN:
                rutas = await self.exportar_obsidian(entry_ids)
                repo.finish_export(self.conn, trabajo.id, result={"paths": rutas})
            elif trabajo.kind == ExportKind.KINDLE:
                await self.enviar_kindle(entry_ids)
                repo.finish_export(self.conn, trabajo.id, result={"enviados": len(entry_ids)})
            else:
                repo.finish_export(
                    self.conn, trabajo.id, error=f"tipo no soportado aquí: {trabajo.kind}"
                )
        except Exception as exc:
            repo.finish_export(self.conn, trabajo.id, error=str(exc))

    # ------------------------------------------------------------- bucles
    async def bucle_refresco(self, stop: asyncio.Event, callback, *, cada: int = 60) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cada)
                return
            except TimeoutError:
                pass
            feeds, nuevas = await self.refrescar()
            if nuevas and callback:
                callback(feeds, nuevas)

    async def bucle_sync(self, stop: asyncio.Event, callback, *, cada: int = 120) -> None:
        if not self.cfg.hub_url:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cada)
                return
            except TimeoutError:
                pass
            resultado = await self.sincronizar()
            if callback:
                callback(resultado)
