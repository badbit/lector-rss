# Transferencia incremental de artículos

Los clientes Python y Flutter reciben artículos añadidos después del snapshot
inicial mediante una ampliación aditiva de `GET /sync/pull`. El cuerpo sigue
fuera del delta: el móvil lo solicita al abrir el artículo y conserva su caché.

## Dos cursores

`since` identifica la última operación de estado recibida. `entries_since`
identifica la última alta examinada en `entry_arrivals`. No son intercambiables:
una ingesta puede crear artículos sin producir ninguna operación de lectura.

La migración Python `005_entry_arrivals.sql` incorpora el archivo existente y
registra automáticamente las nuevas inserciones mediante un trigger SQLite.
La tabla contiene un identificador y una secuencia por artículo; no duplica
el contenido. Cada cliente mantiene el cursor remoto separado de sus propias altas.

Ejemplo de petición:

```text
GET /sync/pull?device_id=movil&since=120&entries_since=30&entries_limit=500
```

| Campo de respuesta | Uso |
|---|---|
| `ops`, `cursor`, `has_more` | Diario y paginación de cambios de estado |
| `entries` | Metadatos de artículos dentro del ámbito |
| `dependencies` | Campos de feeds, carpetas ascendientes y etiquetas necesarios |
| `entry_ops` | Estado actual y etiquetas de esos artículos, con reloj por campo |
| `entries_cursor`, `entries_has_more` | Progreso independiente de las altas |
| `server_lamport` | Reloj observado por el cliente, incluso si la página está vacía |

`entries_limit` admite de 1 a 2000 altas examinadas, con 500 por omisión.
La página puede adjuntar además artículos afectados por el lote del diario:
guardar o marcar como no leído un artículo antiguo puede incorporarlo al ámbito.
Las páginas sin artículos visibles también avanzan, para no quedar atascadas
al atravesar una parte antigua o excluida del archivo.

## Aplicación y recuperación de una página

Se aplican primero las dependencias, se insertan los artículos ausentes y después
se combinan sus estados por reloj de campo. Una entrada existente conserva su
cuerpo local y las marcas más recientes; la cola de subida permanece intacta.
Los datos y ambos cursores se confirman en una transacción. Si falla la página,
se puede solicitar de nuevo sin haber saltado datos ni dejado artículos a medias.

La foto del servidor se lee en una transacción coherente. El hub registra como
confirmado `since`, que es el cursor que el cliente declara tener, y no el de la
respuesta que todavía está transmitiendo. Esto evita compactar basándose en una
entrega que puede fallar por un corte de red.

El snapshot incorpora `entries_cursor`. La base móvil versión 2 añade ese
cursor y la columna `feeds.disabled`; la migración conserva identidad, artículos
y cursor anterior. El arranque móvil también importa `field_clocks` y confirma
foto, cursores y reloj conjuntamente.

## Compatibilidad y límites

- Actualizar hub y clientes para disponer de las altas incrementales. Un cliente
  anterior omite `entries_since` y conserva su comportamiento; un hub anterior
  ignora el parámetro y no proporciona las altas. No hay negociación automática
  de capacidades ni aviso de versión incompatible todavía.
- Los clientes existentes sin cursor de altas recorren el catálogo por páginas
  desde cero, aplicando el ámbito actual. No necesitan borrar su base.
- `max_entries` limita el snapshot inicial. Todavía no existe una política de
  expulsión local para mantener un máximo permanente tras sucesivas descargas.
- Ampliar la ventana temporal o cambiar de hub requiere una estrategia explícita
  de resincronización; los cursores actuales no están asociados a una identidad
  del servidor ni se reinician automáticamente al cambiar el ámbito.
- La recuperación completa por snapshot con una cola local sin subir sigue
  pendiente. Las garantías de conservación anteriores corresponden a las páginas
  incrementales; no equivalen a una fusión de snapshots sobre cambios locales.
- No se replican ediciones posteriores del título o resumen de un artículo ya
  presente, ni se descarga preventivamente su cuerpo para leerlo sin conexión.

Las pruebas de integración Python cubren altas, páginas vacías, límites de ámbito,
entrada de artículos antiguos, dependencias, fallos de aplicación y migración.
Las de Dart cubren el protocolo HTTP, ambos cursores, caché local, conflictos,
importación de relojes y migración de la base móvil.
