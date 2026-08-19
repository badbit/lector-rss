"""Envío de artículos al Kindle por correo (Send to Kindle).

**Se envía EPUB, nunca MOBI.** Amazon retiró el MOBI de Send-to-Kindle en 2022;
desde entonces el servicio acepta EPUB, PDF, DOCX, TXT, RTF y HTML, y convierte
el EPUB a su formato interno (KFX) en el propio dispositivo. Generar un MOBI hoy
solo consigue que el correo rebote, así que aquí no hay ninguna ruta que lo
produzca ni dependencia de KindleGen o Calibre.

Requisitos del lado de Amazon que no se pueden comprobar desde aquí y que son la
causa del 90 % de los fallos:

1. la dirección de `smtp.from_address` tiene que estar dada de alta en la lista
   de remitentes aprobados de la cuenta (Gestionar contenido y dispositivos →
   Preferencias → Configuración de documentos personales);
2. si el SMTP es Gmail, Outlook o similar con verificación en dos pasos, la
   contraseña no es la de la cuenta sino una **contraseña de aplicación**.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import aiosmtplib

from .. import repo
from ..config import MagazineConfig, SmtpConfig
from .epub import EpubArticle, articles_from_entries, build_epub, slugify

# Límite de tamaño del correo. Amazon lo ha ido moviendo (estaba en 25 MB y hoy
# se documenta en 50 MB por mensaje, contando la codificación base64 del
# adjunto): conviene comprobar el valor vigente antes de subirlo. Si el libro se
# pasa no se falla, se trocea en varios envíos.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# base64 engorda el adjunto un 37 %; se reserva margen para eso y para las
# cabeceras del mensaje.
_BASE64_OVERHEAD = 1.40

# Coste aproximado de un artículo dentro del zip, para repartir antes de
# construir nada (el HTML comprime mucho; las imágenes ya vienen comprimidas).
_HTML_RATIO = 0.35
_ARTICLE_OVERHEAD = 2 * 1024


class KindleError(RuntimeError):
    """Fallo de envío con un mensaje que le dice al usuario qué arreglar."""


@dataclass(slots=True)
class KindleResult:
    """Resultado del envío: cuántos correos salieron y con qué dentro."""

    messages: int = 0
    articles: int = 0
    subject: str = ""
    filenames: list[str] = field(default_factory=list)
    bytes_sent: int = 0

    @property
    def split(self) -> bool:
        """True si hubo que trocear el envío en varios correos."""
        return self.messages > 1


# ================================================================== envío alto
async def send_to_kindle(
    conn: sqlite3.Connection,
    entry_ids: Iterable[str],
    smtp: SmtpConfig,
    *,
    title: str | None = None,
    magazine_cfg: MagazineConfig | None = None,
) -> KindleResult:
    """Empaqueta los artículos en un EPUB y lo manda al Kindle.

    Si el libro no cabe en un correo se reparte en varios, cada uno con su
    propio EPUB completo y numerado en el asunto.
    """
    _check_config(smtp)
    cfg = magazine_cfg or MagazineConfig()

    entradas = [e for e in (repo.get_entry(conn, i, with_body=True) for i in entry_ids) if e]
    if not entradas:
        raise KindleError("No hay artículos que enviar: la selección está vacía")

    articulos = await articles_from_entries(
        conn,
        entradas,
        embed_images=cfg.embed_images,
        max_image_width=cfg.max_image_width,
    )
    asunto = title or cfg.title or "Artículos"
    lotes = split_batches(articulos)
    libros = [
        libro
        for lote in lotes
        for libro in _build_within_limit(lote, title=asunto, cfg=cfg)
    ]
    return await _send_books(libros, smtp, subject=asunto, articles=len(articulos))


async def send_epub_file(
    path: Path | str, smtp: SmtpConfig, *, title: str | None = None
) -> KindleResult:
    """Manda un EPUB que ya existe en disco (la revista recién generada)."""
    _check_config(smtp)
    path = Path(path)
    if not path.exists():
        raise KindleError(f"No existe el fichero que se quería enviar: {path}")
    datos = path.read_bytes()
    if _wire_size(len(datos)) > MAX_ATTACHMENT_BYTES:
        raise KindleError(
            f"«{path.name}» ocupa {len(datos) / 1024 / 1024:.1f} MB y no cabe en un correo "
            f"(límite {MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f} MB). Genera la revista con "
            "menos artículos o sin imágenes incrustadas."
        )
    asunto = title or path.stem
    return await _send_books([(datos, path.name)], smtp, subject=asunto, articles=1)


# ============================================================== troceado
def split_batches(
    articles: Sequence[EpubArticle], *, max_bytes: int | None = None
) -> list[list[EpubArticle]]:
    """Reparte los artículos en lotes que quepan, cada uno, en un correo.

    Es una estimación previa: se calcula el peso de cada artículo (texto
    comprimido + imágenes, que ya vienen comprimidas) y se van llenando lotes.
    Lo que se pase después de construir el EPUB de verdad lo parte
    `_build_within_limit`.
    """
    limite = max_bytes if max_bytes is not None else MAX_ATTACHMENT_BYTES
    presupuesto = max(int(limite / _BASE64_OVERHEAD) - 64 * 1024, 1)

    lotes: list[list[EpubArticle]] = []
    actual: list[EpubArticle] = []
    acumulado = 0
    for art in articles:
        peso = _weight(art)
        if actual and acumulado + peso > presupuesto:
            lotes.append(actual)
            actual, acumulado = [], 0
        actual.append(art)
        acumulado += peso
    if actual:
        lotes.append(actual)
    return lotes or [[]]


def _weight(article: EpubArticle) -> int:
    texto = int(len(article.html.encode("utf-8", "replace")) * _HTML_RATIO)
    imagenes = sum(img.size for img in article.images)
    return texto + imagenes + _ARTICLE_OVERHEAD


def _wire_size(raw: int) -> int:
    """Tamaño aproximado del correo una vez codificado el adjunto."""
    return int(raw * _BASE64_OVERHEAD)


def _build_within_limit(
    articles: Sequence[EpubArticle],
    *,
    title: str,
    cfg: MagazineConfig,
    depth: int = 0,
) -> list[tuple[bytes, str]]:
    """Construye el EPUB del lote y, si aun así no cabe, lo parte por la mitad."""
    if not articles:
        return []
    datos = _build(articles, title=title, cfg=cfg)
    if _wire_size(len(datos)) <= MAX_ATTACHMENT_BYTES or len(articles) == 1 or depth > 8:
        # Un solo artículo que no cabe se manda igualmente: que lo rechace
        # Amazon con su motivo es más útil que descartarlo aquí en silencio.
        return [(datos, f"{slugify(title)}.epub")]
    mitad = len(articles) // 2
    return [
        *_build_within_limit(articles[:mitad], title=title, cfg=cfg, depth=depth + 1),
        *_build_within_limit(articles[mitad:], title=title, cfg=cfg, depth=depth + 1),
    ]


def _build(articles: Sequence[EpubArticle], *, title: str, cfg: MagazineConfig) -> bytes:
    return build_epub(
        articles,
        title=title,
        author=cfg.author,
        language=cfg.language,
        css=cfg.css,
        description=f"{len(articles)} artículos enviados con rsscore",
    )


# ============================================================== envío SMTP
async def _send_books(
    books: Sequence[tuple[bytes, str]],
    smtp: SmtpConfig,
    *,
    subject: str,
    articles: int,
) -> KindleResult:
    resultado = KindleResult(subject=subject, articles=articles)
    total = len(books)
    for numero, (datos, nombre) in enumerate(books, start=1):
        asunto = subject if total == 1 else f"{subject} ({numero}/{total})"
        fichero = nombre if total == 1 else f"{Path(nombre).stem}-{numero}.epub"
        mensaje = _build_message(datos, fichero, smtp, subject=asunto)
        await _send(mensaje, smtp)
        resultado.messages += 1
        resultado.filenames.append(fichero)
        resultado.bytes_sent += len(datos)
    return resultado


def _build_message(
    data: bytes, filename: str, smtp: SmtpConfig, *, subject: str
) -> EmailMessage:
    mensaje = EmailMessage()
    mensaje["From"] = smtp.from_address
    mensaje["To"] = smtp.kindle_address
    # Amazon usa el asunto como título del documento cuando el EPUB no trae
    # metadatos; los nuestros sí los traen, pero así el correo se identifica.
    mensaje["Subject"] = subject
    mensaje.set_content(
        f"{subject}\n\nEnviado por rsscore. El adjunto es un EPUB; Send to Kindle lo "
        "convierte al abrirlo en el dispositivo.\n"
    )
    mensaje.add_attachment(
        data, maintype="application", subtype="epub+zip", filename=filename
    )
    return mensaje


async def _send(message: EmailMessage, smtp: SmtpConfig) -> None:
    """Entrega un mensaje traduciendo los fallos típicos a algo accionable."""
    opciones: dict[str, Any] = {
        "hostname": smtp.host,
        "port": smtp.port,
        "timeout": 120,
    }
    if smtp.username:
        opciones["username"] = smtp.username
        opciones["password"] = smtp.password.get_secret_value()
    if smtp.use_ssl:
        # SMTPS: TLS desde el saludo (puerto 465). No se puede combinar con
        # STARTTLS, y aiosmtplib lanza ValueError si se le piden los dos.
        opciones["use_tls"] = True
        opciones["start_tls"] = False
    else:
        opciones["use_tls"] = False
        opciones["start_tls"] = bool(smtp.use_tls)

    try:
        await aiosmtplib.send(message, **opciones)
    except aiosmtplib.SMTPAuthenticationError as exc:
        raise KindleError(
            f"El servidor SMTP rechazó las credenciales de «{smtp.username}» ({exc}). "
            "Con verificación en dos pasos hace falta una contraseña de aplicación, "
            "no la contraseña de la cuenta."
        ) from exc
    except aiosmtplib.SMTPRecipientsRefused as exc:
        raise KindleError(
            f"El servidor no aceptó la dirección «{smtp.kindle_address}» ({exc}). "
            "Comprueba que termina en @kindle.com y que «"
            f"{smtp.from_address}» está en la lista de remitentes aprobados de Amazon."
        ) from exc
    except aiosmtplib.SMTPSenderRefused as exc:
        raise KindleError(
            f"El servidor rechazó el remitente «{smtp.from_address}» ({exc}). "
            "Suele ser que no coincide con la cuenta autenticada del SMTP."
        ) from exc
    except aiosmtplib.SMTPConnectError as exc:
        raise KindleError(
            f"No se pudo conectar con {smtp.host}:{smtp.port} ({exc}). "
            "Revisa host, puerto y si toca STARTTLS (587) o SSL directo (465)."
        ) from exc
    except aiosmtplib.SMTPException as exc:
        raise KindleError(f"El envío al Kindle falló: {exc}") from exc


def _check_config(smtp: SmtpConfig) -> None:
    faltan = [
        nombre
        for nombre, valor in (
            ("smtp.host", smtp.host),
            ("smtp.from_address", smtp.from_address),
            ("smtp.kindle_address", smtp.kindle_address),
        )
        if not valor
    ]
    if faltan:
        raise KindleError(
            "Falta configuración para enviar al Kindle: " + ", ".join(faltan) + ". "
            "La dirección de envío es la @kindle.com del dispositivo y el remitente "
            "tiene que estar aprobado en la cuenta de Amazon."
        )
