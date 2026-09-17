# Reglas, revistas y Kindle

## Qué está disponible

El repositorio ya tenía un motor de reglas y exportadores. Esta iteración
conecta las reglas con la selección de revistas, añade extractos breves,
opciones de escritorio, vista previa HTTP y descargas identificadas por trabajo.
También aplica las reglas en las altas y refrescos manuales del hub, que antes
no utilizaban el motor del refresco programado.

La comparación se basa en la [guía oficial de automatizaciones de Inoreader](https://www.inoreader.com/blog/2026/01/save-time-with-automations.html),
consultada el 7 de septiembre de 2026. No se pretende igualar toda su oferta.

| Función | Estado del lector |
| --- | --- |
| Condiciones combinadas sobre título, contenido, resumen, autor y URL | Implementadas; `all`, `any`, `none`, texto y regex |
| Ámbitos por fuente, carpeta y etiqueta | Implementados; carpetas incluyen ascendientes y admiten nombres o IDs |
| Etiquetar, guardar y marcar leído al ingerir | Implementado; también aplicación manual al archivo con `backfill` |
| Probar reglas sin cambiar artículos | Nuevo: API de vista previa y selección CLI de revistas |
| Ocultar contenido o descartar duplicados entre fuentes como Inoreader | Pendiente; marcar leído no equivale a ocultar |
| Disparadores por cambios de etiquetas o guardados | Pendiente; las reglas automáticas actuales se ejecutan al ingerir |
| Resúmenes redactados o traducciones | Pendiente; los nuevos extractos no usan IA |
| Revista EPUB filtrada y correo al Kindle | Disponible bajo demanda en escritorio, CLI y hub |
| Editor visual de reglas y revistas programadas | Pendiente; las reglas se crean mediante YAML o API |

## Una regla de selección

Guarda este contenido en un YAML propio, por ejemplo `lecturas.yaml`:

```yaml
- id: lecturas-kindle
  name: Lecturas para Kindle
  when:
    any:
      - { field: any, op: contains, value: energía }
      - { field: any, op: contains, value: astronomía }
  then: []
```

Después importa y prueba:

```bash
rss rules import lecturas.yaml
rss digest --rule "Lecturas para Kindle" --days 7 --limit 30 --preview
rss digest --rule "Lecturas para Kindle" --days 7 --limit 30 --out ./revistas/
rss digest --rule "Lecturas para Kindle" --days 7 --limit 30 --brief --words 150 --out ./revistas/
```

`then: []` sirve como filtro sin acciones automáticas. Si deseas etiquetar
los nuevos artículos, puedes usar `then: [{tag: kindle}]`. Para aplicar esas
acciones al archivo ya descargado utiliza `rss backfill --rule lecturas-kindle`;
este comando **sí modifica** marcas/etiquetas y ejecuta las acciones configuradas.

Las opciones `--rule`, `--folder` y `--tag` admiten nombres o IDs y se pueden
repetir. Dentro de cada opción se toma la unión; las distintas opciones se
combinan como restricciones. `--unread` y `--starred` añaden restricciones de
estado. Las carpetas incluyen sus descendientes. Para nombres ambiguos utiliza
el ID; una regla ausente o desactivada genera un error, no una revista sin filtro.

Al seleccionar revistas se evalúan únicamente condiciones y ámbito: no se
ejecutan acciones, ni `stop`, ni se marcan artículos como leídos. El límite cuenta
coincidencias después del filtro. `--preview` nunca genera ni envía, aunque se
añada `--send-to-kindle`. Sin filtros se seleccionan los artículos más recientes.

`--brief` utiliza primero el resumen proporcionado por el feed o, si falta,
el comienzo del cuerpo disponible, hasta `--words` palabras (30–1000). Conserva
el título y el enlace al original y etiqueta el texto como extracto. No sintetiza
ni verifica información. El modo completo usa el cuerpo disponible, que puede
ser parcial si la fuente solo entrega fragmentos; no elimina muros de pago.

Los valores predeterminados están en `magazine` de `config.example.yaml`:
`rules`, `content_mode`, `excerpt_words`, `max_articles` e imágenes. Las opciones
CLI indicadas sustituyen el valor correspondiente, no se guardan. Sin `--brief`
se respeta `content_mode` del archivo de configuración.

Al elegir una carpeta de salida se genera un nombre fechado y, si ya existe,
se añade un identificador. Un `--out archivo.epub` explícito sí reemplaza ese
archivo. Una selección vacía devuelve un error sin generar un libro vacío.

## Escritorio

Abre **Exportar → Generar revista EPUB** (`Ctrl+M`). El diálogo permite elegir
título, contenido completo/extractos, palabras, máximo y reglas activas. Trabaja
sobre la carpeta o búsqueda abierta, no solo sobre las filas resaltadas.
El envío al Kindle requiere marcar explícitamente su casilla.

El diálogo selecciona reglas existentes; aún no permite crearlas ni editarlas.
La generación utiliza el archivo local y no solicita cuerpos ausentes al hub.

## Envío por correo

El formato utilizado es EPUB, admitido por [Send to Kindle](https://digprjsurvey.amazon.co.uk/csad/help/node/G5WYD9SAF7PGXRNA).
No hay conversión a MOBI. Configura `smtp` en tu configuración local: servidor,
puerto, TLS, usuario/contraseña si los requiere, `from_address` y `kindle_address`.
No subas credenciales al repositorio. Comprueba el remitente autorizado y la
dirección de documentos personales siguiendo la [guía de correo de Amazon](https://digprjsurvey.amazon.co.uk/csad/help/node/G7NECT4B4ZWHQ8WV); el proveedor SMTP
puede requerir una contraseña de aplicación.

```bash
rss digest --rule "Lecturas para Kindle" --days 7 --limit 30 --brief --send-to-kindle
```

El archivo se genera antes de enviarse. Si falla SMTP se conserva y se informa
del error. Si la revista supera el límite de tamaño que aplica el exportador,
reduce artículos o desactiva imágenes; el envío de un EPUB existente no lo divide.
Éxito SMTP significa aceptación del correo, no confirma su aparición en el Kindle.
Las pruebas del repositorio simulan SMTP y no envían correos reales.

## API del hub

Usa autenticación Bearer si hay tokens configurados. `POST /rules/preview`
recibe una regla completa, incluso un borrador aún no guardado:

```json
{
  "rule": {
    "name": "Lecturas",
    "when": {"any": [{"field": "any", "op": "contains", "value": "energía"}]},
    "then": []
  },
  "selection": {"limit": 500},
  "sample_size": 20
}
```

Devuelve `revisadas`, `coincidencias` y `muestra` sin escrituras. La muestra admite
1–100 resultados y el recorrido, 1–5000 candidatos. El conteo corresponde solo
a esa selección, no necesariamente a todo el archivo. Para guardar una regla,
utiliza `PUT /rules/{id}` con su definición.

`POST /export/magazine` admite:

```json
{
  "selection": {"unread_only": true, "limit": 30},
  "rules": ["lecturas-kindle"],
  "title": "Mis lecturas",
  "content_mode": "excerpt",
  "excerpt_words": 150,
  "send_to_kindle": false
}
```

`rules` omitido hereda la configuración; `rules: []` la desactiva para esa
petición. Varias reglas se combinan con OR. `selection.offset` salta candidatos
antes de evaluar reglas; para una revista completa comienza en cero.

Devuelve `job`, `articulos`, `fichero`, `download_url` y `enviado_a_kindle`.
Descarga usando `GET /export/download/{job}` con la misma autenticación. Cada
generación tiene una ruta única. Si falla el correo, el error incluye `detail.job`:
el trabajo queda en error, pero el EPUB generado sigue siendo descargable.

La llamada espera a generar y, si se pide, enviar. No es una cola duradera con
reintentos: una caída del proceso puede dejar el trabajo en estado `running`.
Tampoco hay un cursor de última revista; repetir una ventana temporal puede
incluir artículos ya exportados. El contrato completo está en `openapi.json`.

## Siguiente desarrollo sugerido

1. Editor gráfico de reglas con ejemplos y vista previa integrada.
2. Revistas diarias/semanales con historial, zona horaria, deduplicación entre
   ediciones y reintentos SMTP controlados. El planificador actual de revistas
   es un marcador sin implementación de envíos.
3. Resúmenes redactados opcionales: decidir proveedor local/remoto, privacidad,
   límites de coste, conservación de fuentes y caché antes de conectar un modelo.
