# Lector RSS multiplataforma

Lector de noticias para escritorio Linux, terminal y Android, con preparación
para macOS y estado sincronizado entre
dispositivos, sin necesidad de leer en un navegador, y con exportación a
Obsidian, Kindle y revistas EPUB.

## Cómo está montado

```
Escritorio (PySide6) ─┐
Terminal (CLI/TUI) ───┤
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
| TUI `rss tui` (curses, solo teclado) | implementada: lectura, búsqueda, estados, refresco y sincronización |
| macOS | rutas e instalación preparadas; validación nativa pendiente, guía en `docs/macos.md` |
| Escritorio PySide6 (3 paneles, bandeja, atajos) | funcionando |
| Exportadores (Obsidian, Kindle, revista EPUB) | funcionando |
| Importación ZIP de Inoreader | previsualización, respaldo, guardados y procedencia original |
| Cliente Android (Flutter): lectura sin conexión, sincronización, ámbito parcial | funcionando — guía en `docs/android.md` |

La [revisión de desarrollo](docs/desarrollo.md) recoge las limitaciones verificadas
y las siguientes prioridades. Los clientes Python y Flutter ya reciben los
artículos posteriores a su copia inicial, y el escritorio atiende la cola remota
de exportaciones. Sigue pendiente completar la recuperación cuando hay cambios
locales sin subir.

## Instalación

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e './packages/core[dev]' -e ./packages/hub -e ./packages/desktop
source .venv/bin/activate
```

Requiere Python 3.12 o posterior. Para usar exclusivamente CLI/TUI basta instalar
`-e ./packages/core`: no necesita Qt, servidor gráfico ni el paquete del hub.
Puede arrancar sin configuración; en Linux guarda los datos en
`~/.local/share/rss/rss.db`. Para personalizarla, crea `~/.config/rss/` y copia
allí `config.example.yaml` como `config.yaml`, sin sobrescribir una configuración
existente. La instalación en Mac se explica en [docs/macos.md](docs/macos.md).

## Uso rápido

```bash
rss add https://lwn.net/headlines/rss      # acepta también la URL de la web
rss add https://blog.rust-lang.org/ -f Dev # descubre el feed solo
rss refresh                                 # descarga lo que toque
rss unread -n 20
rss search "kernel AND seguridad"           # sintaxis FTS5
rss entries --starred --json                 # archivo y estados para scripts
rss show ID --offline                        # artículo en texto, sin navegador
rss tui                                     # interfaz textual interactiva
rss opml import suscripciones.opml
rss stats

rsshub                                      # arranca el hub
rssdesk                                     # abre el escritorio
```

Para añadir un icono y un acceso al menú de MATE (también compatible con otros
menús freedesktop), ejecuta desde este repositorio:

```bash
.venv/bin/python deploy/install_menu.py --apply
```

El acceso aparece como **Internet → Lector RSS** y usa el Python del entorno
actual. Si mueves el repositorio o cambias de entorno, repite el instalador.
El icono adapta el símbolo RSS de Lucide/Feather, y los íconos de la barra de
herramientas y de los menús son de Lucide; sus licencias ISC/MIT están
incluidas en `packages/desktop/src/rssdesk/assets/LICENSE-icons.txt`.

En el escritorio, **Ver → Barra de herramientas** (o clic derecho sobre la
barra) elige entre solo texto, texto e íconos o solo íconos; la elección se
guarda en `desktop.toolbar_style`. Al pasar el ratón por un botón aparece qué
hace y su atajo.

`rss show ID` descarga el cuerpo desde el hub si falta; `--offline` usa solo lo
local. Consultar no cambia el estado: añade `--mark-read` para marcarlo leído.
Los IDs aparecen en `list`, `entries`, `unread` y `search`; estos comandos
admiten `--json`. Las opciones globales van antes del comando:
`rss --db /ruta/rss.db tui`.

### Interfaz de terminal

`rss tui` muestra fuentes y artículos; Enter abre el texto completo y lo marca
leído. Funciona con la misma configuración y base que `rssdesk`, también sin
conexión cuando el contenido está guardado.

| Tecla | Acción |
|---|---|
| Tab; flechas o j/k | Cambiar panel; navegar |
| Enter; q/Esc | Abrir artículo; volver o salir |
| r; s | Alternar leído; guardado |
| u; g | Filtrar sin leer; guardados |
| / | Buscar con FTS5; consulta vacía limpia el filtro |
| [ / ]; RePág / AvPág | Página anterior / siguiente del archivo |
| Espacio / b | Avanzar / retroceder una pantalla al leer |
| R; S | Refrescar fuentes; sincronizar con el hub |
| a; ? | Añadir fuente; mostrar ayuda |

Las listas cargan páginas de 100 artículos. El terminal necesita al menos 45×10
caracteres; 80×24 permite ver mejor los atajos. La sincronización se solicita
con `S`: los cambios quedan en la cola local hasta entonces. Si hay hub,
las altas y el refresco se realizan allí, salvo que se active explícitamente
`desktop.fetch_locally`. Mientras se realiza una petición de red, se muestra
su estado y la interfaz espera a que termine. Las exportaciones y la gestión
avanzada siguen disponibles en los subcomandos de `rss`.

## Importación de Inoreader

El importador lee directamente el ZIP con `subscriptions.xml` y `starred.json`:

```bash
rss import-inoreader '/ruta/exportacion.zip'           # solo previsualizar
rss import-inoreader '/ruta/exportacion.zip' --apply   # respaldar e importar
```

La previsualización trabaja sobre una copia en memoria y no crea ni modifica
la base de destino. `--apply` crea primero una copia SQLite coherente en
`backups/`, junto a la base, y después importa en una sola transacción.
`--json` devuelve los recuentos, el hash del ZIP y la ruta del respaldo.

Se importan fuentes, carpetas, artículos guardados, contenido disponible,
autores, fechas, estados y enlaces a adjuntos. Cada artículo conserva además
su registro JSON original como procedencia local. El HTML de lectura se sanea;
el original permanece en esa procedencia. Repetir el ZIP no duplica los
artículos ni deshace cambios posteriores de leído/guardado. Las coincidencias
ambiguas con artículos existentes quedan pendientes con un aviso.

Una fuente puede aparecer en varias carpetas de Inoreader. Se conserva una
ubicación principal y se crea una **carpeta inteligente** para cada ubicación
adicional, con el nombre `Carpeta · Inoreader`. Esa vista reúne los feeds
normales de la carpeta y los compartidos, sin duplicar sus artículos, y está
disponible en el árbol del escritorio. La estructura original también se
conserva en la base. Las etiquetas que no son carpetas se importan como etiquetas.

El archivo puede contener solo resúmenes o enlaces: el importador conserva lo
que contiene, sin descargar artículos completos, imágenes ni adjuntos. Las
fuentes de guardados que ya no aparecen en el OPML se mantienen desactivadas.
Los formatos adicionales desconocidos se rechazan para evitar pérdidas silenciosas.

Si usas hub, ejecuta la importación con la configuración y base del **servidor**;
los clientes reciben metadatos y piden los cuerpos al abrir cada artículo. El
comando impide aplicar una importación en un cliente con `hub_url` configurado.
La procedencia y el historial de importaciones son locales a la base importada:
inclúyelos en sus respaldos. Conserva también el ZIP original.

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

Las altas de artículos tienen su propio cursor, independiente del diario de
marcas. Cada página incorpora las suscripciones necesarias y el estado actual
con sus relojes; los clientes confirman ambos cursores en la misma transacción.
El [contrato incremental](docs/sincronizacion-incremental.md) explica la ampliación
del protocolo y sus límites.

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
- **Kindle**: se envía EPUB, uno de los [formatos admitidos por Amazon](https://digprjsurvey.amazon.co.uk/csad/help/node/G5WYD9SAF7PGXRNA).
  El remitente tiene que estar aprobado en Amazon. No se genera MOBI.
- **Revista**: selección de artículos → EPUB 3 con secciones, TOC anidado y
  portada generada.

Las revistas ahora permiten filtrar mediante reglas sin ejecutar sus acciones,
y elegir artículos completos o extractos breves del texto disponible (sin IA).
En el escritorio: **Exportar → Generar revista EPUB** (`Ctrl+M`). Por terminal:

```bash
rss digest --rule "Lecturas para Kindle" --days 7 --limit 30 --preview
rss digest --rule "Lecturas para Kindle" --days 7 --limit 30 --brief --out ./revistas/
# Añadir --send-to-kindle solamente cuando SMTP esté configurado.
```

La regla debe existir previamente; hay un ejemplo en `rules.example.yaml`.
Consulta [reglas, revistas y Kindle](docs/reglas-y-kindle.md) para crearla,
probarla por API, configurar el envío y conocer las diferencias pendientes
respecto a Inoreader. Los extractos no son resúmenes redactados por IA y todavía
no hay programación automática de revistas.

## Pruebas

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
.venv/bin/ruff check packages
```

Incluyen convergencia entre dispositivos con conflictos reales, feeds rotos,
fechas imposibles, GUID duplicados, CLI/TUI y el ciclo completo hub↔cliente por HTTP.
Las pruebas de escritorio usan Qt en modo `offscreen`. Las del arranque por
snapshot comprueban también los relojes por campo, la reversión ante errores,
la coherencia frente a escrituras concurrentes y el límite de la copia parcial.
El workflow `.github/workflows/python.yml` ejecuta ruff y esta suite en Linux y
macOS en cada push y pull request. Una ejecución local en Linux no valida un Mac.

## Licencia

El plan de distribución para Linux, Windows y macOS se encuentra en
[docs/empaquetacion.md](docs/empaquetacion.md). Todavía no se distribuyen
instaladores nativos firmados.

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
