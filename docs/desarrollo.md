# Revisión de desarrollo — 5 de septiembre de 2026

## Arquitectura y estado comprobado

El proyecto reúne cuatro componentes: `rsscore` contiene la ingesta, SQLite,
reglas, sincronización y exportadores; `rsshub` los expone mediante FastAPI;
`rssdesk` proporciona el escritorio PySide6; `mobile/` contiene el cliente Flutter.
La CLI permite utilizar el núcleo de forma independiente.

Hay pruebas de integración con SQLite y el servidor ASGI, además de pruebas de
modelos de escritorio, exportadores y sincronización Dart. El código tiene una
base útil, aunque varios recorridos entre componentes todavía están incompletos.
El estado «funcionando» del README debe leerse junto con estas limitaciones.

## Primera iteración: arranque por snapshot

Se corrigió el arranque por snapshot en el núcleo Python:

- La foto incluye `field_clocks` tanto de entidades estructurales como de los
  artículos incluidos en el ámbito. El importador Python restaura esos relojes:
  una operación atrasada no puede deshacer un estado más reciente de la foto.
  También se conservan los relojes de etiquetas eliminadas.
- La construcción mantiene una única vista de SQLite durante la lectura del
  cursor, los datos y los relojes, incluso cuando otra conexión escribe.
- La importación completa es atómica. Un error revierte datos, relojes e índice
  de búsqueda; se respeta una transacción abierta por el llamador.
- `days=None` incluye también artículos antiguos leídos y sin estrella. Las
  opciones de guardados y no leídos amplían una ventana temporal cuando existe.
- La versión por bloques respeta `max_entries` en el total, mantiene un orden
  estable para fechas iguales y solo confirma el cursor con el bloque final.
  Cada bloque se importa atómicamente; para revertir la descarga entera, el
  llamador debe envolver todos los bloques en su propia transacción.

`field_clocks` es una ampliación aditiva de la versión 1 del snapshot. El cliente
Python admite fotos anteriores sin ese campo, pero esas fotos no proporcionan
la protección de conflictos que ofrecen los relojes exactos. El cambio no migra
ni repara automáticamente copias ya existentes. La importación de estos relojes
en Dart se incorporó en la segunda iteración, descrita más abajo.

También se ajustó la prueba opcional de EPUBCheck para ejecutar `java -jar` cuando
el comando instalado es un enlace a un JAR. En este equipo su ejecución directa
fallaba por el intérprete del sistema; la ejecución con Java validó la revista.

## Verificación de la primera iteración

Se creó `.venv/` y se instalaron los tres paquetes Python en modo editable usando
sus dependencias declaradas. Las comprobaciones se ejecutaron con bases temporales:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check packages
git diff --check
```

Resultado: **122 pruebas Python aprobadas**, incluidas 15 pruebas nuevas de
snapshot; Ruff sin incidencias. Ocho casos de las nuevas pruebas fallaban antes
de la corrección. EPUBCheck terminó sin errores y con una advertencia sobre el
identificador de la revista, que usa el prefijo `urn:uuid:` sin contener un UUID.

En la primera iteración no se compiló ni ejecutó Android: Flutter y su SDK no estaban
disponibles en las rutas documentadas de este equipo. Las pruebas Qt existentes
comprueban modelos en modo `offscreen`; falta una revisión interactiva de la
ventana completa.

## Segunda iteración: artículos nuevos y validación móvil

Se completó la transferencia incremental de artículos en el hub y en los
clientes Python y Dart. Se añadió un cursor de altas, separado del diario de
lectura, con migraciones para el archivo existente y la base móvil. Las páginas
incluyen sus dependencias y recuperan también artículos antiguos que vuelven a
entrar en el ámbito por una estrella o una marca de no leído.

La descarga combina los estados por reloj de campo, conserva los cuerpos
cacheados y confirma datos y cursores en una transacción. Las pruebas incluyen
marcas locales hechas durante la petición, páginas vacías por filtrado,
dependencias repartidas entre páginas y reversión de una página inválida.
El móvil importa ahora los relojes de la foto inicial y confirma foto, cursores
y reloj conjuntamente.

También se corrigieron la confirmación prematura de cursores en el hub y el
identificador UUID del EPUB. El contrato OpenAPI se regeneró a partir del código.
Detalles y límites en [sincronización incremental](sincronizacion-incremental.md).

Verificación de la segunda iteración:

- **135 pruebas Python aprobadas**, incluidas las de interfaz Qt en modo offscreen.
- **16 pruebas Flutter aprobadas**, incluidas cinco nuevas de arranque,
  migración, protocolo y recuperación de páginas.
- Ruff y `flutter analyze` sin incidencias; mypy sin errores en los módulos de
  cliente y transferencia incremental revisados; `pip check` sin dependencias rotas.
- EPUBCheck validó la revista generada sin errores ni advertencias.
- Se instaló temporalmente Flutter 3.35.6 en `/tmp` y se utilizó
  `flutter pub get --enforce-lockfile`, sin modificar el archivo de dependencias.
  El analizador utilizó `ANALYZER_STATE_LOCATION_OVERRIDE` para guardar su estado
  en `/tmp`, ya que el directorio personal está restringido.

No se construyó un APK ni se probó en teléfono o emulador: falta el SDK de Android.
Las pruebas Flutter ejecutan el motor y SQLite en Linux. Estos resultados cubren
los casos probados, no garantizan ausencia de errores en todos los recorridos.

## Tercera iteración: reglas y revistas para Kindle — 7 de septiembre de 2026

Se conectaron las condiciones y ámbitos de reglas existentes con la selección
de revistas, sin ejecutar sus acciones. El límite se aplica después del filtro
y se recorren páginas cuando las primeras no contienen coincidencias. Se
admiten reglas sin acciones; las referencias desconocidas o desactivadas fallan
explícitamente. Carpetas ascendientes y etiquetas se resuelven por nombre o ID.

La nueva CLI `rss digest` permite vista previa, reglas, carpetas, etiquetas,
antigüedad y marcas. La revista admite contenido completo o extractos del texto
fuente, conserva enlaces y no modifica el contenido almacenado. Se añadió un
diálogo de escritorio con esas opciones y envío opcional. El formato sigue
siendo EPUB; no se genera MOBI ni se realizan resúmenes con IA.

El hub ofrece `POST /rules/preview` sin escrituras y amplía `/export/magazine`
con filtros y extractos. Cada generación registra un trabajo descargable y una
ruta única; si falla SMTP se conserva la ruta del EPUB en el trabajo con error.
La generación utiliza una conexión propia en un hilo para no bloquear el bucle
HTTP. OpenAPI se regeneró desde el código.

Correcciones adicionales: los refrescos manuales y las altas del hub aplican
reglas; seleccionar una carpeta vacía ya no devuelve todo el archivo; múltiples
etiquetas no duplican entradas; los EPUB incluyen cuerpos que solo tienen texto
plano. Las revistas generadas en una carpeta no sobrescriben ediciones previas.

Verificación de esta iteración:

- **163 pruebas Python aprobadas**, 28 más que al comenzar, incluidas ingesta
  manual, paginación de reglas, previsualización sin efectos, correo simulado,
  conservación tras fallo SMTP y un clic real sobre el botón del diálogo Qt.
- Ruff, `git diff --check` y `pip check` sin incidencias.
- EPUBCheck validó el EPUB de extractos sin errores ni advertencias; la batería
  completa también ejecutó la prueba del EPUB convencional.
- No se enviaron correos reales ni se comprobó un dispositivo Kindle. No hubo
  cambios en Flutter ni se repitió su validación en esta iteración.

Guía, ejemplos y comparación con Inoreader en [reglas y Kindle](reglas-y-kindle.md).
Próximos pasos de este recorrido: editor visual de reglas, planificación de
revistas con historial/reintentos y resúmenes redactados opcionales. El
planificador de revistas existente todavía es un marcador; la respuesta SMTP
no confirma la entrega final al Kindle. Tampoco se resuelven en esta iteración
los pendientes de sincronización y ejecución remota descritos abajo.

## Integración de la rama alpha 0.2 — 16 de septiembre de 2026

La rama `feature/alpha-0.2` se desarrolló en paralelo a las tres iteraciones
anteriores y resolvió dos prioridades que esta revisión daba por pendientes:

- **Escritorio conectado al hub.** Con un hub configurado, `Backend` le ordena
  el refresco, sincroniza el resultado y le pide el cuerpo de cada artículo al
  abrirlo. `desktop.fetch_locally` conserva el modo autónomo.
- **Cola remota de exportaciones.** `Backend.worker_exportaciones` sincroniza,
  recoge los trabajos de `/export/jobs/next`, obtiene los artículos y cuerpos
  que falten antes de exportar y confirma el resultado en `/export/jobs/finish`.

También incorporó la deduplicación de publicaciones, la exportación desde el
móvil con sincronización periódica mediante WorkManager, la interfaz de
terminal, la importación de Inoreader y un CI de Python para Linux y macOS.

Las dos ramas traían una migración `005`. La de Inoreader pasó a
`006_imports.sql`: una base en la versión 4 recibe ambas en orden. Una base que
se hubiera migrado con la rama antes de la fusión (versión 5 con `import_batches`
y sin `entry_arrivals`) hace fallar la 006 de forma explícita; para repararla hay
que ejecutar `005_entry_arrivals.sql` y fijar `PRAGMA user_version = 6`.

Verificación tras la fusión:

- **214 pruebas Python aprobadas**, Ruff sin incidencias. Las migraciones se
  aplicaron sobre una copia de una base real en la versión 4 y terminaron en la 6
  con `integrity_check` correcto.
- **18 pruebas Flutter aprobadas** y `flutter analyze` sin incidencias, con
  Flutter 3.47.4 y `flutter pub get --enforce-lockfile`. El lockfile de la rama
  no se resuelve con Flutter 3.35.6 ni 3.44: el mínimo es 3.47. No se construyó
  un APK ni se probó en un dispositivo.

## Siguientes prioridades

1. **Arranque y recuperación con cambios locales.** Python y Dart aplican el
   snapshot antes de subir la cola local. Hay que cubrir la recuperación con
   cambios sin subir y evitar que una foto los sobrescriba. También hay que
   asociar los cursores a la identidad del hub y rescatar el archivo al ampliar
   la ventana temporal, además de definir una política de retención local.
2. **Funciones Android pendientes.** UnifiedPush y empaquetado F-Droid, conforme
   a [la guía Android](android.md). La exportación desde el móvil y WorkManager
   llegaron con la rama alpha 0.2.
3. **Validación continua y empaquetado.** El CI ya ejecuta Ruff y pytest en Linux
   y macOS; falta incorporar Flutter, verificar instalación de wheels y ejecutar
   una prueba de lectura y sincronización
   en un dispositivo Android real. Las dependencias Python solo fijan versiones mínimas: conviene
   registrar un conjunto reproducible para despliegues.
