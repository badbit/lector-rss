# Cliente Android (pendiente)

El cliente de Android es un **cliente delgado**: no parsea feeds, no genera EPUB
ni envía correos. Todo eso vive en el hub. La app solo lista, lee, marca, etiqueta
y dispara acciones.

## Contrato

`docs/openapi.json` es el esquema OpenAPI generado por el hub (36 endpoints). Se
regenera con:

```bash
rsshub --port 8799 --no-scheduler &
curl -s localhost:8799/openapi.json -o docs/openapi.json
```

De ahí se pueden generar los modelos Dart automáticamente.

## Endpoints que necesita el cliente

| Para | Endpoints |
|---|---|
| Arranque | `POST /sync/register`, `GET /sync/snapshot` |
| Sincronizar | `POST /sync/push`, `GET /sync/pull`, `GET /sync/stream` (SSE) |
| Leer | `GET /entries`, `GET /entries/{id}` |
| Marcar | `POST /entries/read`, `POST /entries/star`, `POST /entries/tag` |
| Suscripciones | `GET /feeds`, `POST /feeds`, `GET /folders` |
| Buscar | `GET /search?q=` (sintaxis FTS5) |
| Exportar | `POST /export/obsidian` (se encola para el escritorio), `POST /export/kindle`, `POST /export/magazine` |

Autenticación: `Authorization: Bearer <token>` con uno de los `hub.tokens`.

## Puntos a respetar en la implementación

1. **Replicación parcial.** El móvil declara su `SyncScope` en `/sync/register`:
   ventana de días, más lo guardado y lo no leído. Sin eso se traería un archivo
   de años. El hub filtra el delta en el servidor.

2. **Arranque por snapshot, no por diario.** La primera vez se llama a
   `/sync/snapshot`; reproducir el `change_log` entero no es viable.

3. **La cola local no se vacía hasta que el hub confirma.** Es lo que hace que un
   viaje en metro sin cobertura no pierda ningún «marcar como leído».

4. **Notificaciones por UnifiedPush**, con ntfy como distribuidor: sin Google Play
   Services y publicable en F-Droid. El hub hace POST a ntfy cuando una regla
   dispara. Aviso: si ntfy solo es accesible por Tailscale, las alertas llegan
   únicamente con la VPN activa.

5. **Exportar a Obsidian se encola**, no se hace en el móvil: la bóveda está en el
   escritorio. `POST /export/obsidian` crea un trabajo con `target: desktop` que el
   escritorio materializa al arrancar.

## Stack sugerido

Flutter + `drift` (SQLite tipado) para el espejo local, `workmanager` para la
sincronización periódica, `flutter_local_notifications` + `unifiedpush`.
