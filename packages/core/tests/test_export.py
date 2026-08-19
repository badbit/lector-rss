"""Pruebas de los exportadores: Obsidian, EPUB, Kindle y revista.

No se simula nada que se pueda hacer de verdad: la base es una SQLite real, el
EPUB se vuelve a abrir con `ebooklib` y el frontmatter se relee con
`yaml.safe_load`. Lo único que se sustituye es la red (las imágenes viajan como
`data:` URI) y el servidor SMTP.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from ebooklib import ITEM_DOCUMENT, epub
from PIL import Image
from rsscore import repo
from rsscore.config import MagazineConfig, ObsidianConfig, SmtpConfig
from rsscore.db import open_db
from rsscore.ids import hash_content, hash_guid, now_ms
from rsscore.models import Entry, EntrySelection, Feed

DIA = 86_400_000


# ==================================================================== utilería
@pytest.fixture
def conn(tmp_path: Path):
    conexion = open_db(tmp_path / "rss.db", device_name="pruebas")
    yield conexion
    conexion.close()


def crear_feed(conn, titulo: str, url: str = "") -> Feed:
    feed = Feed(url=url or f"https://{titulo.lower().replace(' ', '')}.example/feed", title=titulo)
    return repo.add_feed(conn, feed)


def crear_entrada(
    conn,
    feed: Feed,
    titulo: str,
    *,
    html: str = "<p>Cuerpo del artículo.</p>",
    url: str | None = None,
    autor: str = "Ana Autora",
    publicado: int | None = None,
) -> Entry:
    url = url if url is not None else f"https://ejemplo.test/{abs(hash(titulo))}"
    entrada = Entry(
        feed_id=feed.id,
        guid_hash=hash_guid(feed.id, url or titulo),
        content_hash=hash_content(titulo, html),
        url=url,
        title=titulo,
        author=autor,
        summary=titulo,
        published_at=publicado if publicado is not None else now_ms(),
        body_html=html,
        body_text=titulo,
    )
    repo.insert_entry(conn, entrada)
    return entrada


def png_data_uri(lado: int = 8, color: tuple[int, int, int] = (30, 90, 180)) -> str:
    """Imagen incrustada en el propio HTML: exporta con imágenes y sin red."""
    buf = BytesIO()
    Image.new("RGB", (lado, lado), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def smtp_de_pruebas() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.ejemplo.test",
        port=587,
        username="yo@ejemplo.test",
        password="secreta",
        use_tls=True,
        from_address="yo@ejemplo.test",
        kindle_address="yo_1234@kindle.com",
    )


# ==================================================================== Obsidian
def test_obsidian_frontmatter_sobrevive_a_un_titulo_hostil(conn, tmp_path: Path):
    """Un titular con `:`, comillas y `/` no puede romper ni el YAML ni el nombre."""
    from rsscore.export.obsidian import export_to_obsidian

    feed = crear_feed(conn, "Blog de Rust")
    titulo = 'Rust 1.80: "async" y el 50/50 de {rendimiento}'
    entrada = crear_entrada(
        conn,
        feed,
        titulo,
        html=(
            "<h2>Novedades</h2><p>Un <strong>texto</strong> con "
            '<a href="https://rust.example">enlace</a>.</p>'
            "<blockquote><p>Una cita textual.</p></blockquote>"
            '<pre><code class="language-rust">fn main() {\n    println!("hola");\n}\n</code></pre>'
        ),
        url="https://rust.example/1-80",
    )
    tag = repo.get_or_create_tag(conn, "programación")
    repo.tag_entry(conn, entrada.id, tag.id)

    cfg = ObsidianConfig(vault_path=tmp_path / "vault")
    rutas = export_to_obsidian(conn, [entrada.id], cfg)

    assert len(rutas) == 1
    nota = rutas[0]
    assert nota.exists()
    # El nombre no puede llevar separadores de ruta ni caracteres prohibidos.
    assert "/" not in nota.name
    assert ":" not in nota.name
    assert '"' not in nota.name
    assert nota.parent == tmp_path / "vault" / "Clippings"

    texto = nota.read_text(encoding="utf-8")
    bloque = texto.split("\n---", 2)[0].removeprefix("---")
    meta = yaml.safe_load(bloque)

    assert meta["title"] == titulo                       # el titular, íntegro
    assert meta["source"] == "https://rust.example/1-80"
    assert meta["feed"] == "Blog de Rust"
    assert meta["author"] == "Ana Autora"
    assert meta["published"].startswith("20")            # ISO 8601
    assert "T" in meta["published"] and meta["published"].endswith("+00:00")
    assert meta["created"]
    assert meta["tags"] == ["programación"]
    assert titulo in meta["aliases"]

    cuerpo = texto.split("\n---", 2)[-1]
    assert "## Novedades" in cuerpo                      # heading_style ATX
    assert "> Una cita textual." in cuerpo               # la cita sobrevive
    assert "```rust" in cuerpo                           # y el bloque de código
    assert 'println!("hola");' in cuerpo


def test_obsidian_reexportar_no_duplica(conn, tmp_path: Path):
    from rsscore.export.obsidian import export_to_obsidian

    feed = crear_feed(conn, "Diario")
    entrada = crear_entrada(conn, feed, "Una noticia", url="https://diario.test/1")
    cfg = ObsidianConfig(vault_path=tmp_path / "vault")

    primera = export_to_obsidian(conn, [entrada.id], cfg)
    segunda = export_to_obsidian(conn, [entrada.id], cfg)
    tercera = export_to_obsidian(conn, [entrada.id], cfg)

    assert primera == segunda == tercera
    notas = list((tmp_path / "vault" / "Clippings").glob("*.md"))
    assert len(notas) == 1, [n.name for n in notas]


def test_obsidian_numera_articulos_distintos_con_el_mismo_nombre(conn, tmp_path: Path):
    """Mismo título y misma fecha, pero otro artículo: se numera, no se pisa."""
    from rsscore.export.obsidian import export_to_obsidian

    feed = crear_feed(conn, "Agencia")
    momento = now_ms()
    uno = crear_entrada(
        conn, feed, "Titular repetido", url="https://agencia.test/a", publicado=momento
    )
    dos = crear_entrada(
        conn, feed, "Titular repetido", url="https://agencia.test/b", publicado=momento
    )

    cfg = ObsidianConfig(vault_path=tmp_path / "vault")
    rutas = export_to_obsidian(conn, [uno.id, dos.id], cfg)

    assert len(set(rutas)) == 2
    assert rutas[1].stem.endswith("-2")
    for ruta in rutas:
        meta = yaml.safe_load(ruta.read_text(encoding="utf-8").split("\n---", 2)[0][3:])
        assert meta["source"] in {"https://agencia.test/a", "https://agencia.test/b"}


def test_obsidian_recorta_nombres_larguisimos(conn, tmp_path: Path):
    from rsscore.export.obsidian import MAX_FILENAME_BYTES, export_to_obsidian

    feed = crear_feed(conn, "Interminable")
    entrada = crear_entrada(conn, feed, "Ñandú " * 120, url="https://largo.test/1")

    cfg = ObsidianConfig(vault_path=tmp_path / "vault")
    ruta = export_to_obsidian(conn, [entrada.id], cfg)[0]

    assert len(ruta.stem.encode("utf-8")) <= MAX_FILENAME_BYTES
    assert ruta.exists()
    # El titular completo se conserva en el alias aunque el fichero se recorte.
    meta = yaml.safe_load(ruta.read_text(encoding="utf-8").split("\n---", 2)[0][3:])
    assert meta["aliases"][0].startswith("Ñandú")


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("../../etc/passwd", "etc-passwd"),          # nada de salirse de la carpeta
        ("con/barra", "con-barra"),
        ('comillas "dobles"', "comillas dobles"),
        ("   .oculto  ", "oculto"),                  # ni ficheros ocultos ni espacios
        ("", "sin-titulo"),
        ("...", "sin-titulo"),
    ],
)
def test_saneado_de_nombres(entrada: str, esperado: str):
    from rsscore.export.obsidian import sanitize_filename

    resultado = sanitize_filename(entrada)
    assert resultado == esperado
    assert "/" not in resultado
    assert "\x00" not in resultado


def test_saneado_recorta_por_bytes_no_por_caracteres():
    from rsscore.export.obsidian import MAX_FILENAME_BYTES, sanitize_filename

    # «ñ» ocupa dos bytes: recortar por longitud dejaría el nombre a 400 bytes.
    nombre = sanitize_filename("ñ" * 400)
    assert len(nombre.encode("utf-8")) <= MAX_FILENAME_BYTES
    assert nombre.encode("utf-8").decode("utf-8") == nombre   # no se parte a medias


def test_obsidian_plantilla_propia_y_nombre_configurable(conn, tmp_path: Path):
    from rsscore.export.obsidian import export_to_obsidian

    plantilla = tmp_path / "mi_plantilla.md.j2"
    plantilla.write_text("---\n{{ frontmatter }}---\n\nDE: {{ feed }}\n", encoding="utf-8")

    feed = crear_feed(conn, "Semanal")
    entrada = crear_entrada(conn, feed, "Portada", url="https://semanal.test/1")

    cfg = ObsidianConfig(
        vault_path=tmp_path / "vault",
        notes_subdir="Notas",
        template=str(plantilla),
        filename_template="{{ feed }}-{{ title }}",
    )
    ruta = export_to_obsidian(conn, [entrada.id], cfg)[0]

    assert ruta.name == "Semanal-Portada.md"
    assert "DE: Semanal" in ruta.read_text(encoding="utf-8")


def test_obsidian_descarga_imagenes_a_los_adjuntos(conn, tmp_path: Path):
    from rsscore.export.obsidian import export_to_obsidian

    feed = crear_feed(conn, "Con fotos")
    entrada = crear_entrada(
        conn,
        feed,
        "Artículo ilustrado",
        html=f'<p>Mira:</p><img src="{png_data_uri(16)}" alt="foto"/>',
        url="https://fotos.test/1",
    )
    cfg = ObsidianConfig(vault_path=tmp_path / "vault", download_images=True)
    ruta = export_to_obsidian(conn, [entrada.id], cfg)[0]

    adjuntos = list((tmp_path / "vault" / "Clippings" / "attachments").glob("*"))
    assert len(adjuntos) == 1
    # El enlace es relativo desde la carpeta de notas, no absoluto.
    assert f"attachments/{adjuntos[0].name}" in ruta.read_text(encoding="utf-8")


# ======================================================================== EPUB
def _revista_de_prueba(conn) -> tuple[Feed, Feed]:
    ciencia = crear_feed(conn, "Ciencia Hoy")
    letras = crear_feed(conn, "Letras")
    base = now_ms()
    crear_entrada(
        conn,
        ciencia,
        "Agujeros negros",
        html=f'<p>Uno.</p><img src="{png_data_uri(12)}" alt="ilustración"/>',
        publicado=base,
        url="https://ciencia.test/1",
    )
    crear_entrada(conn, ciencia, "Neutrinos", publicado=base - DIA, url="https://ciencia.test/2")
    crear_entrada(conn, letras, "Sobre Borges", publicado=base - 2 * DIA, url="https://letras.test/1")
    return ciencia, letras


def test_epub_tiene_capitulos_toc_anidado_y_portada(conn, tmp_path: Path):
    from rsscore.export.magazine import build_magazine

    _revista_de_prueba(conn)
    resultado = build_magazine(
        conn,
        EntrySelection(limit=50),
        MagazineConfig(title="Mi revista", author="rsscore", language="es"),
        out_path=tmp_path,
    )

    assert resultado.path.exists()
    libro = epub.read_epub(str(resultado.path))

    assert libro.get_metadata("DC", "title")[0][0] == "Mi revista"
    assert libro.get_metadata("DC", "language")[0][0] == "es"
    assert libro.get_metadata("DC", "creator")[0][0] == "rsscore"

    # TOC anidado: una sección por feed, con sus artículos dentro.
    secciones = {
        nodo[0].title: [c.title for c in nodo[1]]
        for nodo in libro.toc
        if isinstance(nodo, tuple)
    }
    assert secciones == {
        "Ciencia Hoy": ["Agujeros negros", "Neutrinos"],
        "Letras": ["Sobre Borges"],
    }

    documentos = [i.get_name() for i in libro.get_items_of_type(ITEM_DOCUMENT)]
    assert "cover.xhtml" in documentos          # portada generada con Pillow
    assert "sumario.xhtml" in documentos        # portadilla con las cifras
    assert "nav.xhtml" in documentos            # navegación EPUB 3
    assert sum(1 for n in documentos if n.startswith("art_")) == 3

    nombres = {i.get_name() for i in libro.get_items()}
    assert "toc.ncx" in nombres                 # NCX para lectores antiguos
    assert "portada.jpg" in nombres
    assert any(n.endswith(".jpg") and n.startswith("art") is False for n in nombres)

    # La portada es un JPEG que Pillow puede volver a abrir.
    portada = next(i for i in libro.get_items() if i.get_name() == "portada.jpg")
    with Image.open(BytesIO(portada.get_content())) as imagen:
        assert imagen.size == (1200, 1600)

    # La imagen del artículo viaja dentro y se referencia por su nombre plano.
    capitulo = next(
        i for i in libro.get_items_of_type(ITEM_DOCUMENT) if i.get_name() == "art_0001.xhtml"
    ).get_content().decode()
    assert "<img" in capitulo
    assert "data:image" not in capitulo
    # El enlace a la hoja de estilo se comprueba sobre el fichero tal cual está
    # en el zip: al releer, ebooklib reconstruye la cabecera y lo pierde.
    with zipfile.ZipFile(resultado.path) as contenedor:
        assert "estilo.css" in contenedor.read("EPUB/art_0001.xhtml").decode()
        assert contenedor.read("mimetype") == b"application/epub+zip"

    sumario = next(
        i for i in libro.get_items_of_type(ITEM_DOCUMENT) if i.get_name() == "sumario.xhtml"
    ).get_content().decode()
    assert "Artículos: 3" in sumario
    assert "Lectura" in sumario


def test_epub_pasa_epubcheck_si_esta_instalado(conn, tmp_path: Path):
    from rsscore.export.magazine import build_magazine

    binario = shutil.which("epubcheck")
    if not binario:
        pytest.skip("epubcheck no está instalado en el sistema")

    _revista_de_prueba(conn)
    resultado = build_magazine(
        conn, EntrySelection(limit=50), MagazineConfig(), out_path=tmp_path
    )
    proceso = subprocess.run(
        [binario, str(resultado.path)], capture_output=True, text=True, check=False
    )
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr


def test_epub_es_un_contenedor_bien_formado(conn, tmp_path: Path):
    """Sustituto parcial de epubcheck: estructura del zip y XML válido.

    No cubre todo lo que valida epubcheck, pero sí lo que se rompe de verdad al
    tocar las plantillas: un XHTML mal cerrado o un manifiesto que promete un
    fichero que no está.
    """
    from xml.etree import ElementTree

    from rsscore.export.magazine import build_magazine

    _revista_de_prueba(conn)
    resultado = build_magazine(
        conn, EntrySelection(limit=50), MagazineConfig(), out_path=tmp_path
    )

    with zipfile.ZipFile(resultado.path) as contenedor:
        nombres = contenedor.namelist()
        # `mimetype` va el primero y sin comprimir: lo exige la especificación.
        assert nombres[0] == "mimetype"
        assert contenedor.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert "META-INF/container.xml" in nombres

        for nombre in nombres:
            if nombre.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ElementTree.fromstring(contenedor.read(nombre))

        opf = ElementTree.fromstring(contenedor.read("EPUB/content.opf"))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        for item in opf.findall(".//opf:manifest/opf:item", ns):
            assert f"EPUB/{item.get('href')}" in nombres, item.get("href")


def test_epub_sin_secciones_sale_con_indice_plano(conn):
    from rsscore.export.epub import EpubArticle, build_epub

    datos = build_epub(
        [EpubArticle(title="Suelto", html="<p>Hola</p>")],
        title="Sin secciones",
        author="rsscore",
        language="es",
    )
    assert datos[:2] == b"PK"
    assert len(datos) > 1000


def test_portada_no_falla_sin_tipografias_del_sistema(monkeypatch):
    """En un contenedor `slim` no hay ni una TTF: la portada tiene que salir igual."""
    from rsscore.export import epub as mod

    monkeypatch.setattr(mod, "_FONT_CANDIDATES", {True: (), False: ()})
    datos = mod.render_cover("Título de la revista", subtitle="hoy", articles=7)
    with Image.open(BytesIO(datos)) as imagen:
        assert imagen.size == (1200, 1600)


def test_portada_no_falla_con_un_pillow_antiguo(monkeypatch):
    """Antes de Pillow 10.1 `load_default` no acepta tamaño; tampoco puede reventar."""
    from PIL import ImageFont
    from rsscore.export import epub as mod

    original = ImageFont.load_default

    def sin_tamano(size=None):
        if size is not None:
            raise TypeError("load_default() got an unexpected keyword argument 'size'")
        return original()

    monkeypatch.setattr(mod, "_FONT_CANDIDATES", {True: (), False: ()})
    monkeypatch.setattr(ImageFont, "load_default", sin_tamano)
    datos = mod.render_cover("Sin tipografías", subtitle="hoy", articles=1)
    with Image.open(BytesIO(datos)) as imagen:
        assert imagen.size == (1200, 1600)


# ====================================================================== Kindle
@pytest.fixture
def smtp_espia(monkeypatch):
    """Sustituye `aiosmtplib.send` y guarda los mensajes que se habrían enviado."""
    import aiosmtplib

    enviados: list[tuple] = []

    async def falso_send(message, **kwargs):
        enviados.append((message, kwargs))
        return ({}, "250 Ok")

    monkeypatch.setattr(aiosmtplib, "send", falso_send)
    return enviados


async def test_kindle_envia_un_epub_adjunto(conn, smtp_espia):
    from rsscore.export.kindle import send_to_kindle

    feed = crear_feed(conn, "Ciencia Hoy")
    entradas = [
        crear_entrada(conn, feed, f"Artículo {i}", url=f"https://c.test/{i}") for i in range(3)
    ]

    resultado = await send_to_kindle(
        conn, [e.id for e in entradas], smtp_de_pruebas(), title="Lectura del sábado"
    )

    assert resultado.messages == 1
    assert resultado.articles == 3
    assert len(smtp_espia) == 1

    mensaje, opciones = smtp_espia[0]
    assert mensaje["From"] == "yo@ejemplo.test"
    assert mensaje["To"] == "yo_1234@kindle.com"
    assert mensaje["Subject"] == "Lectura del sábado"
    assert opciones["hostname"] == "smtp.ejemplo.test"
    assert opciones["port"] == 587
    assert opciones["start_tls"] is True        # STARTTLS
    assert opciones["use_tls"] is False         # y no SMTPS

    adjuntos = list(mensaje.iter_attachments())
    assert len(adjuntos) == 1
    assert adjuntos[0].get_content_type() == "application/epub+zip"
    assert adjuntos[0].get_filename().endswith(".epub")
    contenido = adjuntos[0].get_payload(decode=True)
    assert contenido[:2] == b"PK"               # es un zip de verdad
    libro = epub.read_epub(BytesIO(contenido))
    assert len(list(libro.get_items_of_type(ITEM_DOCUMENT))) >= 3


async def test_kindle_usa_smtps_cuando_toca(conn, smtp_espia):
    from rsscore.export.kindle import send_to_kindle

    feed = crear_feed(conn, "Ciencia Hoy")
    entrada = crear_entrada(conn, feed, "Uno", url="https://c.test/1")
    smtp = smtp_de_pruebas()
    smtp.use_ssl, smtp.use_tls, smtp.port = True, False, 465

    await send_to_kindle(conn, [entrada.id], smtp)

    _, opciones = smtp_espia[0]
    assert opciones["use_tls"] is True
    assert opciones["start_tls"] is False       # nunca los dos a la vez


async def test_kindle_trocea_lo_que_no_cabe(conn, smtp_espia, monkeypatch):
    from rsscore.export import kindle

    feed = crear_feed(conn, "Río de texto")
    cuerpo = "<p>" + ("palabra " * 20_000) + "</p>"     # ~140 kB por artículo
    entradas = [
        crear_entrada(conn, feed, f"Largo {i}", html=cuerpo, url=f"https://l.test/{i}")
        for i in range(4)
    ]
    ids = [e.id for e in entradas]

    # Con el límite real caben todos en un solo correo.
    completo = await kindle.send_to_kindle(conn, ids, smtp_de_pruebas())
    assert completo.messages == 1
    assert completo.split is False

    smtp_espia.clear()
    # Con un límite ridículo hay que trocear, no fallar.
    monkeypatch.setattr(kindle, "MAX_ATTACHMENT_BYTES", 20_000)
    troceado = await kindle.send_to_kindle(conn, ids, smtp_de_pruebas(), title="Tocho")

    assert troceado.messages > 1
    assert troceado.split is True
    assert len(smtp_espia) == troceado.messages
    asuntos = [m["Subject"] for m, _ in smtp_espia]
    assert asuntos[0] == f"Tocho (1/{troceado.messages})"
    ficheros = [next(iter(m.iter_attachments())).get_filename() for m, _ in smtp_espia]
    assert len(set(ficheros)) == len(ficheros)   # nombres distintos, no se pisan
    assert all(f.endswith(".epub") for f in ficheros)


async def test_kindle_reparte_los_lotes_por_tamano(conn):
    from rsscore.export.epub import EpubArticle
    from rsscore.export.kindle import split_batches

    articulos = [EpubArticle(title=f"A{i}", html="<p>" + "x" * 30_000 + "</p>") for i in range(6)]
    lotes = split_batches(articulos, max_bytes=40_000)

    assert len(lotes) > 1
    assert sum(len(lote) for lote in lotes) == 6


async def test_kindle_avisa_de_lo_que_falta_configurar(conn):
    from rsscore.export.kindle import KindleError, send_to_kindle

    with pytest.raises(KindleError) as fallo:
        await send_to_kindle(conn, ["lo-que-sea"], SmtpConfig())
    mensaje = str(fallo.value)
    assert "smtp.host" in mensaje
    assert "kindle.com" in mensaje


async def test_kindle_traduce_el_fallo_de_autenticacion(conn, monkeypatch):
    import aiosmtplib
    from rsscore.export.kindle import KindleError, send_to_kindle

    async def rechaza(*_args, **_kwargs):
        raise aiosmtplib.SMTPAuthenticationError(535, "5.7.8 Username and Password not accepted")

    monkeypatch.setattr(aiosmtplib, "send", rechaza)
    feed = crear_feed(conn, "Ciencia Hoy")
    entrada = crear_entrada(conn, feed, "Uno", url="https://c.test/1")

    with pytest.raises(KindleError) as fallo:
        await send_to_kindle(conn, [entrada.id], smtp_de_pruebas())
    assert "contraseña de aplicación" in str(fallo.value)


async def test_kindle_envia_un_epub_ya_hecho(conn, tmp_path, smtp_espia):
    from rsscore.export.kindle import send_epub_file
    from rsscore.export.magazine import build_magazine

    _revista_de_prueba(conn)
    revista = build_magazine(
        conn, EntrySelection(limit=10), MagazineConfig(), out_path=tmp_path
    )

    resultado = await send_epub_file(revista.path, smtp_de_pruebas(), title="Mi revista")

    assert resultado.messages == 1
    mensaje, _ = smtp_espia[0]
    assert mensaje["Subject"] == "Mi revista"
    assert next(iter(mensaje.iter_attachments())).get_filename() == revista.path.name


# ===================================================================== revista
def test_revista_agrupa_por_feed_y_respeta_max_articles(conn, tmp_path: Path):
    from rsscore.export.magazine import build_magazine

    ciencia = crear_feed(conn, "Ciencia Hoy")
    letras = crear_feed(conn, "Letras")
    base = now_ms()
    for i in range(4):
        crear_entrada(
            conn, ciencia, f"Ciencia {i}", publicado=base - i * 1000, url=f"https://c.test/{i}"
        )
    for i in range(3):
        crear_entrada(
            conn, letras, f"Letras {i}", publicado=base - 10_000 - i * 1000,
            url=f"https://l.test/{i}",
        )

    cfg = MagazineConfig(title="Semanal", max_articles=5, embed_images=False)
    resultado = build_magazine(conn, EntrySelection(limit=100), cfg, out_path=tmp_path)

    assert resultado.articles == 5                      # el tope manda
    assert dict(resultado.sections) == {"Ciencia Hoy": 4, "Letras": 1}
    assert resultado.stats.articles == 5
    assert resultado.stats.words > 0
    assert resultado.stats.minutes >= 1

    libro = epub.read_epub(str(resultado.path))
    secciones = [n[0].title for n in libro.toc if isinstance(n, tuple)]
    assert secciones == ["Ciencia Hoy", "Letras"]       # alfabético y estable


def test_revista_nombra_el_fichero_con_la_fecha(conn, tmp_path: Path):
    import datetime as dt

    from rsscore.export.magazine import build_magazine

    _revista_de_prueba(conn)
    dia = dt.date(2026, 8, 19)
    resultado = build_magazine(
        conn, EntrySelection(limit=10), MagazineConfig(), out_path=tmp_path, date=dia
    )

    assert resultado.path.name == "revista-2026-08-19.epub"
    assert resultado.size_bytes == resultado.path.stat().st_size


def test_revista_vacia_lo_dice_claro(conn, tmp_path: Path):
    from rsscore.export.magazine import build_magazine

    with pytest.raises(ValueError, match="ningún artículo"):
        build_magazine(conn, EntrySelection(limit=10), MagazineConfig(), out_path=tmp_path)


# ====================================================================== cola
async def test_job_de_obsidian_se_ejecuta_y_se_cierra(conn, tmp_path: Path):
    from rsscore.config import Config
    from rsscore.export.jobs import run_export_job
    from rsscore.models import ExportJob, ExportKind, ExportStatus

    feed = crear_feed(conn, "Diario")
    entrada = crear_entrada(conn, feed, "Noticia encolada", url="https://diario.test/9")

    cfg = Config(obsidian=ObsidianConfig(vault_path=tmp_path / "vault"))
    job = ExportJob(kind=ExportKind.OBSIDIAN, target="desktop", params={"entry_ids": [entrada.id]})
    repo.enqueue_export(conn, job)
    tomado = repo.claim_export(conn, "desktop")
    assert tomado is not None

    await run_export_job(conn, tomado, cfg)

    guardado = repo.list_exports(conn)[0]
    assert guardado.status is ExportStatus.DONE
    assert guardado.error is None
    assert guardado.result["count"] == 1
    assert Path(guardado.result["paths"][0]).exists()


async def test_job_de_revista_genera_el_fichero(conn, tmp_path: Path):
    """La revista es síncrona y descarga imágenes: no puede anidar bucles."""
    from rsscore.config import Config
    from rsscore.export.jobs import run_export_job
    from rsscore.models import ExportJob, ExportKind, ExportStatus

    _revista_de_prueba(conn)
    cfg = Config(magazine=MagazineConfig(title="Del hub"))
    job = ExportJob(
        kind=ExportKind.MAGAZINE,
        target="hub",
        params={"selection": {"limit": 20}, "out_path": str(tmp_path)},
    )
    repo.enqueue_export(conn, job)
    tomado = repo.claim_export(conn, "hub")

    await run_export_job(conn, tomado, cfg)

    guardado = repo.list_exports(conn)[0]
    assert guardado.status is ExportStatus.DONE, guardado.error
    assert guardado.result["articles"] == 3
    ruta = Path(guardado.result["path"])
    assert ruta.exists() and ruta.parent == tmp_path
    assert epub.read_epub(str(ruta)).get_metadata("DC", "title")[0][0] == "Del hub"


async def test_job_fallido_guarda_un_error_legible(conn, tmp_path: Path):
    from rsscore.config import Config
    from rsscore.export.jobs import run_export_job
    from rsscore.models import ExportJob, ExportKind, ExportStatus

    cfg = Config(obsidian=ObsidianConfig(vault_path=tmp_path / "vault"))
    job = ExportJob(kind=ExportKind.OBSIDIAN, target="desktop", params={"entry_ids": []})
    repo.enqueue_export(conn, job)
    tomado = repo.claim_export(conn, "desktop")

    await run_export_job(conn, tomado, cfg)

    guardado = repo.list_exports(conn)[0]
    assert guardado.status is ExportStatus.ERROR
    assert "ningún artículo" in guardado.error


async def test_worker_loop_vacia_la_cola_y_para(conn, tmp_path: Path):
    import asyncio

    from rsscore.config import Config
    from rsscore.export.jobs import worker_loop
    from rsscore.models import ExportJob, ExportKind

    feed = crear_feed(conn, "Diario")
    entradas = [
        crear_entrada(conn, feed, f"Noticia {i}", url=f"https://diario.test/{i}") for i in range(2)
    ]
    for entrada in entradas:
        repo.enqueue_export(
            conn,
            ExportJob(
                kind=ExportKind.OBSIDIAN, target="desktop", params={"entry_ids": [entrada.id]}
            ),
        )

    cfg = Config(obsidian=ObsidianConfig(vault_path=tmp_path / "vault"))
    parar = asyncio.Event()

    async def detener_cuando_acabe():
        while len([j for j in repo.list_exports(conn) if j.status == "done"]) < 2:
            await asyncio.sleep(0.01)
        parar.set()

    hechos, _ = await asyncio.gather(
        worker_loop(conn, cfg, "desktop", poll_seconds=0.01, stop_event=parar),
        detener_cuando_acabe(),
    )
    assert hechos == 2
    assert len(list((tmp_path / "vault" / "Clippings").glob("*.md"))) == 2
