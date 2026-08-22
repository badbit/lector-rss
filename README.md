# Lector RSS multiplataforma

Lector de noticias para escritorio Linux y Android con estado sincronizado entre
dispositivos, sin necesidad de leer en un navegador, y con exportación a
Obsidian, Kindle y revistas EPUB.

## Cómo está montado

```
Escritorio (PySide6) ─┐
                      ├─► HUB headless (FastAPI, JSON + SSE) ─► SMTP → Kindle
Android (Flutter)  ───┘        SQLite WAL + FTS5              └─► ntfy → avisos
```

El **hub** es la fuente de verdad: descarga cada feed una sola vez para todos los
dispositivos, aplica las reglas al ingerir, genera los EPUB y manda los correos.
Solo habla JSON y SSE — no tiene interfaz web. Vive detrás de Tailscale.

Los **clientes** trabajan sin conexión: cada uno lleva su propia copia SQLite y
una cola de cambios que sube cuando puede.

## Estado del proyecto

| Parte | Estado |
|---|---|
| Núcleo: fetch, parseo, dedup, FTS5, archivo | funcionando |
| Webs sin feed: raspado, vigilancia, feeds ocultos | funcionando |
| Sincronización: diario, LWW por campo, ámbito, snapshot, compactación | funcionando |
| Hub: API JSON + SSE, planificador, tokens | funcionando |
| Reglas, alertas, ntfy, carpetas inteligentes | funcionando |
| CLI `rss` | funcionando |
| Escritorio PySide6 (3 paneles, bandeja, atajos) | funcionando |
| Exportadores (Obsidian, Kindle, revista EPUB) | funcionando |
| Cliente Android (Flutter): lectura sin conexión, sincronización, ámbito parcial | funcionando — guía en `docs/android.md` |

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/core[dev] -e packages/hub -e packages/desktop
cp config.example.yaml ~/.config/rss/config.yaml   # y edítalo
```

## Uso rápido

```bash
rss add https://lwn.net/headlines/rss      # acepta también la URL de la web
rss add https://blog.rust-lang.org/ -f Dev # descubre el feed solo
rss refresh                                 # descarga lo que toque
rss unread -n 20
rss search "kernel AND seguridad"           # sintaxis FTS5
rss opml import suscripciones.opml
rss stats

rsshub                                      # arranca el hub
rssdesk                                     # abre el escritorio
```

## Webs sin feed RSS

Se puede seguir un sitio que no publica feed, de dos formas. Lo que sale de ahí
es una entrada normal del archivo: la indexa la búsqueda, la ven las reglas, se
sincroniza y se exporta igual que cualquier otra.

```bash
# 1. Antes de nada se buscan feeds que la web tiene pero no enlaza
rss add https://news.ycombinator.com/     # encuentra /rss aunque no esté en el <head>

# 2. Raspado: la página es un listado de artículos
rss scrape https://ejemplo.org/blog --preview   # enseña qué extraería
rss scrape https://ejemplo.org/blog -f Tec      # lo da de alta

# 3. Vigilancia: la página no es una lista y solo interesa saber si cambia
rss watch https://ejemplo.org/descargas --selector "main" --ignore ".contador"
```

El selector se deduce solo: se busca qué estructura se repite en la página y se
propone la que más parece un listado de artículos, con una muestra de titulares
para poder juzgarlo antes de dar nada de alta. Con `--selector` se indica a mano.

Cuatro cosas que conviene saber:

- **Un feed siempre es mejor.** Por eso se agotan primero las rutas habituales
  (`/feed`, `/rss.xml`, `/index.xml`…): un feed no se rompe cuando rediseñan la
  web, y el raspado sí. Cuando se rompe, el error lo dice con esas palabras.
- **Webs hechas con JavaScript no funcionan**, porque solo se descarga el HTML.
  El sistema lo detecta y lo dice, en vez de dejar un feed vacío sin explicación.
- **Cortesía**: las fuentes raspadas nunca se visitan más de una vez cada media
  hora, ni siquiera con el jitter.
- En vigilancia, `--ignore` es importante: sin él, un contador de visitas o un
  «actualizado el…» dispararían la alarma en cada visita.

## Sincronización

El modelo es un diario de cambios con reloj de Lamport. Cada escritura genera una
operación `(entidad, id, campo, valor, lamport, device_id)`; los conflictos se
resuelven **campo a campo** con last-write-wins, desempatando por `device_id`.
Eso hace que el resultado sea idéntico en todos los nodos sin coordinación, y que
aplicar el mismo lote dos veces o en distinto orden dé siempre lo mismo.

Tres detalles que importan con un archivo permanente:

- **Operaciones huérfanas**: si llega el estado de un artículo que este
  dispositivo aún no ha descargado, se aparca en `sync_pending` y se reaplica
  cuando el artículo aparece. Descartarlo perdería el cambio para siempre.
- **Replicación parcial**: el móvil declara un `SyncScope` (ventana de días +
  guardados + no leídos) y el hub filtra el delta en el servidor.
- **Compactación**: el diario se colapsa cada noche dejando la última operación
  de cada campo, nunca por encima del cursor del cliente más rezagado.

## Publicaciones duplicadas

Durante cada ingesta se conserva una sola publicación cuando, dentro del mismo
feed RSS y dentro de una ventana de siete días, coincide la URL canónica
(ignorando fragmentos y parámetros de rastreo) o coincide exactamente el
contenido. El primer refresco también limpia duplicados que ya estuvieran
archivados.

Antes de borrar una copia se fusionan sus estados: prevalecen «sin leer» y
«guardado», se unen las etiquetas y se conserva el cuerpo más completo. La baja
viaja en el diario para que Android elimine también su copia local. Artículos
iguales procedentes de feeds diferentes se conservan, porque pueden representar
coberturas distintas de una misma noticia.

## Reglas

`~/.config/rss/rules.yaml`, cargado con `rss rules import`:

```yaml
- name: Alertas Rust
  when:
    any:
      - { field: title,   op: matches,  value: '(?i)\brust\b' }
      - { field: content, op: contains, value: cargo }
  scope: { folders: [Dev] }
  then:
    - { tag: rust }
    - { star: true }
    - { notify: { priority: high } }
```

Las comparaciones ignoran mayúsculas y acentos: `energia` encuentra «Energía».
Si una regla dispara con cuarenta artículos en un mismo refresco se envía **un**
aviso agrupado, no cuarenta.

## Exportación

- **Obsidian**: Markdown con frontmatter YAML. El escritorio escribe en la
  bóveda; desde el móvil la acción se encola en el hub y el escritorio la
  materializa al arrancar.
- **Kindle**: se envía **EPUB**, no MOBI — Amazon retiró MOBI de Send-to-Kindle
  en 2022. El remitente tiene que estar aprobado en Amazon.
- **Revista**: selección de artículos → EPUB 3 con secciones, TOC anidado y
  portada generada.

## Pruebas

```bash
.venv/bin/python -m pytest packages/core/tests packages/hub/tests -q
```

Incluyen convergencia entre dispositivos con conflictos reales, feeds rotos,
fechas imposibles, GUID duplicados y el ciclo completo hub↔cliente por HTTP.

## Licencia

**AGPL-3.0-or-later** (texto completo en [`LICENSE`](LICENSE)).

No es una elección estética: `ebooklib`, con el que se generan los EPUB, es
AGPL-3.0-or-later, y `PySide6` se usa bajo LGPL-3.0-only. La única licencia que
cubre el conjunto sin contradicciones es la AGPL, que además es la que encaja
con lo que esto es: un servidor de sincronización que quien lo modifique y lo
sirva a otros tendrá que publicar.

El hub cumple el artículo 13 anunciando la URL de las fuentes en la descripción
de la API y en `/health`. **Si modificas el hub y se lo sirves a alguien más,
cambia `SOURCE_URL` en `packages/hub/src/rsshub/app.py` por la de tu versión.**

Licencias de las dependencias directas, por si empaquetas para Debian: AGPL-3.0+
(`ebooklib`), LGPL-3.0 (`PySide6`), Apache-2.0 (`trafilatura`, `python-dateutil`)
y BSD/MIT el resto. Todas son DFSG-libres. Aviso de empaquetado: la AGPL-3 **no**
está en `/usr/share/common-licenses`, así que `debian/copyright` tiene que llevar
el texto entero en lugar de una referencia.
