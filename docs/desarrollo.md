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

## Cambios de esta iteración

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
ni repara automáticamente copias ya existentes. El cliente Dart todavía debe
incorporar la importación de estos relojes.

También se ajustó la prueba opcional de EPUBCheck para ejecutar `java -jar` cuando
el comando instalado es un enlace a un JAR. En este equipo su ejecución directa
fallaba por el intérprete del sistema; la ejecución con Java validó la revista.

## Verificación

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

No se compiló ni ejecutó Android: Flutter y el SDK de Android no estaban
disponibles en las rutas documentadas de este equipo. Las pruebas Qt existentes
comprueban modelos en modo `offscreen`; falta una revisión interactiva de la
ventana completa.

## Siguientes prioridades

1. **Transferencia incremental de artículos nuevos.** `repo.insert_entry` inserta
   el artículo sin publicar su contenido o metadatos en el diario; `PullResponse`
   solo transporta cambios de estado y estructura. Los clientes obtienen artículos
   en el snapshot inicial, pero no incorporan los posteriores mediante el delta.
   Se reprodujo por HTTP: después de añadir un segundo artículo al hub y volver a
   sincronizar, el hub tenía dos y el cliente Python seguía teniendo uno. Hace
   falta un protocolo paginado para esas altas, integrado en Python y Dart,
   incluyendo artículos antiguos que entren posteriormente en el ámbito.
2. **Arranque y recuperación con cambios locales.** Python y Dart aplican el
   snapshot antes de subir la cola local. Hay que cubrir la recuperación con
   cambios sin subir y evitar que una foto los sobrescriba. En Dart también falta
   importar `field_clocks` y confirmar foto, cursor y reloj en la misma transacción.
3. **Cola remota de Obsidian.** El hub ofrece `/export/jobs/next` y
   `/export/jobs/finish`, pero `Backend.worker_exportaciones` consulta solamente
   su base local. Hace falta recoger y confirmar los trabajos remotos y obtener
   sus artículos y cuerpos antes de exportar.
4. **Escritorio conectado al hub.** El refresco de `Backend` sigue usando un
   `Ingestor` local aunque haya hub configurado. Hay que definir y comprobar el
   funcionamiento independiente y el conectado para que el segundo delegue la
   descarga y recupere los cuerpos del servidor.
5. **Funciones Android pendientes.** Tras cerrar la sincronización: exportación
   desde el móvil, WorkManager, UnifiedPush y empaquetado F-Droid, conforme a
   [la guía Android](android.md).
6. **Validación continua y empaquetado.** Incorporar CI para Python y Flutter,
   verificar instalación de wheels y revisar el identificador EPUB señalado por
   EPUBCheck. Las dependencias Python solo fijan versiones mínimas: conviene
   registrar un conjunto reproducible para despliegues.
