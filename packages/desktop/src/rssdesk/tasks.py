"""Trabajo en segundo plano: refresco, sincronización, exportaciones y alertas.

Todo corre en el bucle de asyncio que `qasync` comparte con Qt, así que se puede
tocar la interfaz directamente desde estas corrutinas sin marshalling entre
hilos. Lo que no se puede es bloquear: cualquier cosa lenta tiene que ser await.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable

import httpx
from rsscore import repo
from rsscore.config import Config
from rsscore.models import Entry, ExportJob, ExportKind
from rsscore.notify import Notification, build_notifier, coalesce

log = logging.getLogger("rssdesk.tasks")


class Backend:
    """Envuelve el núcleo para que la ventana no sepa de asyncio ni de SQL."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.notificador = build_notifier(cfg.notify)
        self._refrescando = False
        self._http_client = http_client

    @contextlib.asynccontextmanager
    async def _hub(self, *, timeout: float = 60) -> AsyncIterator[httpx.AsyncClient]:
        """Cliente autenticado; la inyección permite probar contra FastAPI real."""
        if self._http_client is not None:
            yield self._http_client
            return
        token = self.cfg.hub_token.get_secret_value()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(
            base_url=self.cfg.hub_url.rstrip("/"), headers=headers, timeout=timeout
        ) as client:
            yield client

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
            if not self.cfg.desktop_fetches_locally:
                return await self._refrescar_en_hub(todos=todos, feed_id=feed_id)

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

    async def _refrescar_en_hub(
        self, *, todos: bool = False, feed_id: str | None = None
    ) -> tuple[int, int]:
        """Ordena el refresco al hub y baja inmediatamente su resultado."""
        if not self.cfg.hub_url:
            return (0, 0)
        ruta = f"/feeds/{feed_id}/refresh" if feed_id else "/feeds/refresh"
        async with self._hub() as client:
            response = await client.post(ruta, params={"force": todos} if not feed_id else None)
            response.raise_for_status()
            data = response.json()
        await self.sincronizar()
        return (int(data.get("feeds", 1)), int(data.get("nuevas", 0)))

    def _hook_reglas(self, avisos: list[Notification]) -> Callable | None:
        try:
            from rsscore.rules import RuleEngine, load_rules, make_ingest_hook
        except ImportError:
            return None
        reglas = load_rules(self.conn)
        if not reglas:
            return None
        return make_ingest_hook(RuleEngine(reglas), collector=avisos)

    async def suscribirse(self, url: str, folder_id: str | None = None) -> str:
        """Da de alta en el hub cuando existe; en modo autónomo usa el núcleo."""
        if self.cfg.desktop_fetches_locally:
            from rsscore.ingest import Ingestor

            async with Ingestor(self.conn, self.cfg) as ingestor:
                feed = await ingestor.add_by_url(url, folder_id=folder_id)
            return feed.display_title

        async with self._hub() as client:
            response = await client.post("/feeds", json={"url": url, "folder_id": folder_id})
            response.raise_for_status()
            data = response.json()
        await self.sincronizar()
        return str(data.get("title") or url)

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

        cliente = SyncClient(
            self.conn,
            self.cfg.hub_url,
            self.cfg.hub_token.get_secret_value(),
            client=self._http_client,
        )
        try:
            stats = await cliente.sync_once(name=self.cfg.device_name or "escritorio")
            return str(stats)
        except Exception as exc:
            log.warning("Sincronización fallida: %s", exc)
            return f"error: {exc}"
        finally:
            await cliente.aclose()

    # --------------------------------------------------------- exportación
    async def cargar_articulo(self, entry_id: str) -> Entry | None:
        """Trae y cachea el cuerpo que el diario omite deliberadamente."""
        entry = repo.get_entry(self.conn, entry_id, with_body=True)
        if entry is None or not self.cfg.hub_url:
            return entry
        cached = self.conn.execute(
            "SELECT 1 FROM entry_bodies WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if cached:
            return entry
        async with self._hub() as client:
            response = await client.get(f"/entries/{entry_id}")
            response.raise_for_status()
            data = response.json()
        repo.update_entry_body(
            self.conn,
            entry_id,
            html=data.get("body_html"),
            text=data.get("body_text"),
        )
        return repo.get_entry(self.conn, entry_id, with_body=True)

    async def _asegurar_cuerpos(self, entry_ids: list[str]) -> None:
        if not self.cfg.hub_url:
            return
        for entry_id in dict.fromkeys(entry_ids):
            if await self.cargar_articulo(entry_id) is None:
                raise RuntimeError(f"El hub ya no contiene el artículo {entry_id}")

    async def exportar_obsidian(self, entry_ids: list[str]) -> list[str]:
        from rsscore.export.obsidian import export_to_obsidian

        if not self.cfg.obsidian.vault_path:
            raise RuntimeError("Configura obsidian.vault_path en el config.yaml")
        await self._asegurar_cuerpos(entry_ids)
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
    async def procesar_exportacion_pendiente(self, *, target: str = "desktop") -> bool:
        """Procesa un trabajo local o, con hub, uno reclamado por HTTP."""
        if not self.cfg.hub_url:
            trabajo = repo.claim_export(self.conn, target)
            if trabajo is None:
                return False
            resultado, error = await self._resultado_exportacion(trabajo)
            repo.finish_export(self.conn, trabajo.id, result=resultado, error=error)
            return True

        # El trabajo puede referirse a una entrada recién ingerida. Se sincroniza
        # antes de reclamarlo para no dejarlo en ``running`` por no tenerla aún.
        sincronizacion = await self.sincronizar()
        if sincronizacion.startswith("error:"):
            raise RuntimeError(sincronizacion)
        async with self._hub() as client:
            response = await client.get("/export/jobs/next", params={"target": target})
            response.raise_for_status()
            data = response.json()
            if data is None:
                return False
            trabajo = ExportJob.model_validate(data)
            resultado, error = await self._resultado_exportacion(trabajo)
            terminado = await client.post(
                "/export/jobs/finish",
                json={"job_id": trabajo.id, "result": resultado, "error": error},
            )
            terminado.raise_for_status()
        return True

    async def worker_exportaciones(self, stop: asyncio.Event, *, target: str = "desktop") -> None:
        """Materializa lo que el móvil encoló: la bóveda de Obsidian está aquí.

        El hub no puede escribir en ella, así que el escritorio hace de brazo
        ejecutor cuando está encendido.
        """
        while not stop.is_set():
            try:
                if not await self.procesar_exportacion_pendiente(target=target):
                    await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fallo en la cola de exportaciones")
                await asyncio.sleep(10)

    async def _resultado_exportacion(self, trabajo: ExportJob) -> tuple[dict, str | None]:
        try:
            entry_ids = trabajo.params.get("entry_ids", [])
            if trabajo.kind == ExportKind.OBSIDIAN:
                rutas = await self.exportar_obsidian(entry_ids)
                return {"paths": rutas, "count": len(rutas)}, None
            elif trabajo.kind == ExportKind.KINDLE:
                await self.enviar_kindle(entry_ids)
                return {"enviados": len(entry_ids)}, None
            return {}, f"tipo no soportado aquí: {trabajo.kind}"
        except Exception as exc:
            from rsscore.export.jobs import describe_error

            return {}, describe_error(exc)

    # ------------------------------------------------------------- bucles
    async def bucle_refresco(self, stop: asyncio.Event, callback, *, cada: int = 60) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cada)
                return
            except TimeoutError:
                pass
            # Con hub, su planificador es el único que visita las fuentes. El
            # bucle de sincronización de este cliente traerá las novedades.
            if not self.cfg.desktop_fetches_locally:
                continue
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
