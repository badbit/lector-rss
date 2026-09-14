# Distribución del lector

La instalación actual usa paquetes Python separados: `rsscore` (CLI y TUI),
`rssdesk` (escritorio) y `rsshub` (servidor). El icono SVG y sus avisos de licencia
viven dentro del paquete de escritorio para poder viajar con él, con
identificador `org.badbit.LectorRSS`.

## Entrega actual

- Linux: instalación desde fuentes y acceso de usuario al menú mediante
  `deploy/install_menu.py --apply`, sin necesidad de permisos de administrador.
- macOS: instalación desde fuentes, descrita en [macos.md](macos.md), aún pendiente
  de ejecución nativa. No hay `.app` o `.dmg` firmado.
- Windows: todavía no está validado ni empaquetado. La TUI usa `curses`, que
  exige una adaptación específica; no debe anunciarse como compatible todavía.

## Próximas fases propuestas

| Plataforma | Primer artefacto propuesto | Comprobaciones necesarias |
|---|---|---|
| Linux | `.deb` para Mint/Ubuntu; después AppImage | Bibliotecas Qt, icono/menú, actualización y conservación de SQLite |
| Windows | Aplicación con instalador `.exe` | Qt, FTS5, rutas AppData, icono `.ico`, consola/GUI y firma |
| macOS | `.app` dentro de `.dmg` | Apple Silicon/Intel, icono `.icns`, firma, notarización y permisos |

Una opción para estudiar es `pyside6-deploy`, la herramienta de Qt para
[distribuir aplicaciones PySide6 en las tres plataformas](https://doc.qt.io/qtforpython-6.8/deployment/index.html).
También existe la ruta documentada con
[PyInstaller](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html).
La elección queda pendiente de un prototipo con este proyecto y todas sus
dependencias; no se ha añadido un sistema de empaquetado sin validarlo.

Cada artefacto deberá construirse y comprobarse en su sistema de destino,
incluyendo las migraciones SQL, plantillas Jinja, SVG y licencias. La suite debe
cubrir importación/reimportación, búsqueda FTS5, cuerpos comprimidos, exportación,
sincronización y una apertura real del escritorio. Para Windows habrá que
decidir además si se incluye la TUI mediante `windows-curses` o mediante otro
backend probado.

El empaquetado deberá conservar las bases del usuario durante las actualizaciones,
hacer respaldos antes de las migraciones y distribuir las fuentes y avisos
requeridos por las licencias de la aplicación y sus dependencias. El hub seguirá
siendo opcional: instalar el lector no debe instalar ni arrancar un servidor.

## Acceso MATE actual

El instalador genera un `.desktop` que sigue la
[especificación freedesktop](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html).
Usa rutas absolutas al intérprete del entorno y al repositorio; copia el SVG
al tema `hicolor` del usuario y su licencia a la carpeta de datos de la aplicación.
No necesita activar un entorno desde el menú. Una instalación futura como paquete
sustituirá esas rutas del checkout por las rutas del paquete instalado.
