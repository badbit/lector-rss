# Cliente Android

Cliente **delgado**: no descarga feeds, no genera EPUB ni envía correos. Todo eso
vive en el hub. La app replica una ventana del archivo, la lee sin conexión y
sincroniza el estado.

Código en `mobile/`. Flutter 3.35 / Dart 3.9, `sqflite` con SQL directo.

## Qué hay hecho

| | |
|---|---|
| Conexión al hub | dirección + token, con prueba de conexión |
| Arranque | `POST /sync/register` y `GET /sync/snapshot` |
| Sincronización | `push` y `pull` con reloj por campo y cola de salida |
| Navegación | carpetas, feeds y contadores de no leídos |
| Lectura | lista paginada, artículo sin JavaScript, cuerpo cacheado |
| Marcar | leído y guardado, con gestos en la lista |
| Recuperación | borrar la copia local y volver a traérsela del hub |

Pendiente: avisos por UnifiedPush, sincronización en segundo plano con
WorkManager, exportar a Obsidian/Kindle desde el móvil y empaquetado para F-Droid.

## Compilar

La cadena vive entera en `$HOME`, sin tocar paquetes del sistema:

```bash
export JAVA_HOME="$HOME/.local/share/jdk"
export ANDROID_HOME="$HOME/Android/Sdk"
export PATH="$HOME/.local/share/flutter/bin:$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH"

cd mobile
flutter pub get
flutter test          # motor de sincronización, sin dispositivo
flutter analyze
flutter build apk --debug
```

Las pruebas de Dart abren la misma base SQLite en el escritorio con
`sqflite_common_ffi`, así que las propiedades que hacen converger el sistema
—idempotencia, conmutatividad, determinismo del conflicto— se comprueban sin
emulador ni teléfono.

## Probar contra un hub de verdad

En el emulador, `10.0.2.2` es el alias del loopback del anfitrión:

```bash
rss --config demo.yaml add https://lwn.net/headlines/newrss
rss --config demo.yaml refresh --all
python -m rsshub --config demo.yaml --no-scheduler   # escuchando en 0.0.0.0

emulator -avd rss_test -no-audio -no-boot-anim -gpu swiftshader_indirect
adb install -r build/app/outputs/flutter-apk/app-debug.apk
```

En Ajustes: dirección `http://10.0.2.2:8787` y uno de los `hub.tokens`.

Para mirar la base del móvil por dentro (solo en compilaciones de depuración):

```bash
adb shell run-as org.rsscore.rssmovil cat databases/rss.db > movil.db
```

## Contrato

`docs/openapi.json` es el esquema que genera el hub. Se regenera con:

```bash
rsshub --port 8799 --no-scheduler &
curl -s localhost:8799/openapi.json -o docs/openapi.json
```

Autenticación: `Authorization: Bearer <token>` con uno de los `hub.tokens`.

| Para | Endpoints |
|---|---|
| Arranque | `POST /sync/register`, `GET /sync/snapshot` |
| Sincronizar | `POST /sync/push`, `GET /sync/pull`, `GET /sync/stream` (SSE) |
| Leer | `GET /entries`, `GET /entries/{id}` |
| Marcar | `POST /entries/read`, `POST /entries/star`, `POST /entries/tag` |
| Suscripciones | `GET /feeds`, `POST /feeds`, `GET /folders` |
| Buscar | `GET /search?q=` (sintaxis FTS5) |
| Exportar | `POST /export/obsidian` (se encola para el escritorio), `POST /export/kindle`, `POST /export/magazine` |

## Puntos que hay que respetar

1. **Replicación parcial.** El móvil declara su `SyncScope` al registrarse:
   ventana de días, más lo guardado y lo no leído. Sin eso se traería un archivo
   de años. El hub filtra el delta en el servidor.

   Ojo con el filtro: una operación que SACA una entrada del ámbito —marcar como
   leído algo más viejo que la ventana, quitarle la estrella— tiene que viajar
   igualmente, o el cliente se queda desincronizado para siempre. Está resuelto en
   `filter_ops_for_scope` y cubierto por pruebas; conviene no perderlo de vista al
   tocar el ámbito.

2. **Arranque por snapshot, no por diario.** La primera vez se llama a
   `/sync/snapshot`; reproducir el `change_log` entero no es viable.

3. **Reloj por campo, no por fila.** Si `read` y `starred` compartieran reloj, el
   orden de llegada decidiría cuál sobrevive y los dispositivos no convergerían.

4. **La cola local no se vacía hasta que el hub confirma.** Es lo que hace que un
   viaje en metro sin cobertura no pierda ningún «marcar como leído».

5. **Notificaciones por UnifiedPush**, con ntfy como distribuidor: sin Google Play
   Services y publicable en F-Droid. El hub hace POST a ntfy cuando una regla
   dispara. Aviso: si ntfy solo es accesible por Tailscale, las alertas llegan
   únicamente con la VPN activa.

6. **Exportar a Obsidian se encola**, no se hace en el móvil: la bóveda está en el
   escritorio. `POST /export/obsidian` crea un trabajo con `target: desktop` que el
   escritorio materializa al arrancar.
