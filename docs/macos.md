# Instalación en macOS

El objetivo es macOS moderno, tanto Intel como Apple Silicon. No se promete
compatibilidad con las versiones antiguas 10.x llamadas Mac OS X. La CLI y la
TUI necesitan Python 3.12 o posterior con `curses` y SQLite FTS5; el escritorio
añade los requisitos de la versión de PySide6/Qt instalada.

Las versiones actuales de Qt 6.10/6.11 requieren macOS 13 o posterior y admiten
`x86_64` y `arm64`, según [las plataformas oficiales de Qt](https://doc.qt.io/qt-6/supported-platforms.html).
La interfaz textual utiliza [curses de Python](https://docs.python.org/3/howto/curses.html).
Consulta también [la instalación oficial de Python en macOS](https://docs.python.org/3/using/mac.html).

## Terminal: instalación desde el código

Instala una versión reciente de Python desde python.org. Desde la carpeta del
repositorio, sustituyendo `python3.12` por tu intérprete si instalaste otro más reciente:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ./packages/core
python -c 'import curses, sqlite3; c = sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE probe USING fts5(text)"); print("curses y FTS5 disponibles")'
rss --help
rss add https://lwn.net/headlines/rss
rss refresh --all
rss tui
```

No hace falta instalar el hub ni Qt para usar la terminal. Funciona en una
terminal interactiva con UTF-8 (Terminal.app, por ejemplo); en un pipe utiliza
`rss entries --json` o `rss show ID --offline`.

## Configuración y datos

Sin configuración explícita se usan estas rutas:

| Contenido | Ruta |
|---|---|
| Configuración | `~/Library/Application Support/rss/config.yaml` |
| Base SQLite | `~/Library/Application Support/rss/rss.db` |
| Revistas | `~/Library/Application Support/rss/revistas/` |

Puedes arrancar sin crear el YAML. Para personalizarlo, copia
`config.example.yaml` a la ruta anterior y edita `hub_url`, `hub_token` y
`device_name` si usas sincronización. El ejemplo omite `db_path` para utilizar
la ruta de cada plataforma; puedes indicar una ruta absoluta propia.

`RSS_CONFIG`, `RSS_DB`, `XDG_CONFIG_HOME` y `XDG_DATA_HOME` conservan su prioridad.
Una configuración preexistente en `~/.config/rss/config.yaml` sigue usándose.
También se conserva la antigua base relativa `data/rss.db` cuando existe en
el directorio de ejecución y no se ha elegido otra base: para independizarse
de ese directorio, indica su ruta absoluta en `db_path`. No se mueven bases
ni configuraciones automáticamente. `rss stats` muestra la base realmente abierta.

CLI, TUI y escritorio pueden usar la misma base local del mismo dispositivo.
Cada equipo necesita su propia base e identidad; usa el hub para sincronizar
equipos, sin compartir un SQLite activo mediante Syncthing, iCloud o Dropbox.

## Escritorio opcional

Dentro del mismo entorno virtual:

```sh
python -m pip install -e ./packages/desktop
rssdesk
```

Esta entrega prepara la instalación desde fuentes. Aún no incluye una aplicación
`.app`/`.dmg` firmada ni notarizada. La bandeja y las notificaciones dependen de
la sesión gráfica y requieren una prueba real en Mac.

## Validación y límites

```sh
python -m pip install -e './packages/core[dev]' -e ./packages/hub -e ./packages/desktop
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

Se añaden pruebas de rutas macOS y un workflow de GitHub Actions para
`ubuntu-latest` y `macos-latest`. Hasta ejecutarlo, su resultado en macOS está
pendiente; tampoco sustituye abrir `rss tui` y `rssdesk` en un Mac real.
El runner de macOS valida la arquitectura que proporciona GitHub, no ambas
arquitecturas automáticamente.
