"""Entidades de dominio compartidas por el núcleo, el hub y los clientes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .ids import new_id, now_ms


# ------------------------------------------------------------------- entidades
class Folder(BaseModel):
    id: str = Field(default_factory=new_id)
    parent_id: str | None = None
    name: str
    position: int = 0
    deleted: bool = False
    lamport: int = 0
    device_id: str = ""
    updated_at: int = Field(default_factory=now_ms)


class Feed(BaseModel):
    id: str = Field(default_factory=new_id)
    folder_id: str | None = None
    url: str
    site_url: str | None = None
    title: str = ""
    custom_title: str | None = None
    description: str | None = None
    icon_url: str | None = None
    # descarga (local a cada nodo, no se sincroniza)
    etag: str | None = None
    last_modified: str | None = None
    last_fetch_at: int | None = None
    last_success_at: int | None = None
    next_fetch_at: int = 0
    interval_seconds: int = 1800
    error_count: int = 0
    last_error: str | None = None
    disabled: bool = False
    fetch_full_text: bool = False
    keep_unread: bool = False
    # origen: feed RSS/Atom, raspado de una web o vigilancia de una página
    source_kind: str = "feed"
    source_config_json: str = "{}"
    watch_hash: str | None = None      # local a cada nodo, como `etag`
    # sincronización
    deleted: bool = False
    lamport: int = 0
    device_id: str = ""
    updated_at: int = Field(default_factory=now_ms)

    @property
    def display_title(self) -> str:
        return self.custom_title or self.title or self.url


class Entry(BaseModel):
    id: str = Field(default_factory=new_id)
    feed_id: str
    guid_hash: str
    content_hash: str
    url: str | None = None
    title: str = ""
    author: str | None = None
    summary: str | None = None
    published_at: int = Field(default_factory=now_ms)
    updated_at: int | None = None
    fetched_at: int = Field(default_factory=now_ms)
    has_body: bool = False
    full_text_at: int | None = None
    enclosure_url: str | None = None
    enclosure_type: str | None = None
    # no persistidos en `entries`; se rellenan al leer
    body_html: str | None = None
    body_text: str | None = None


class EntryState(BaseModel):
    entry_id: str
    read: bool = False
    starred: bool = False
    read_at: int | None = None
    star_at: int | None = None
    lamport: int = 0
    device_id: str = ""


class Tag(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    color: str | None = None
    deleted: bool = False
    lamport: int = 0
    device_id: str = ""


# ------------------------------------------------------------- sincronización
class Entity(StrEnum):
    FEED = "feed"
    FOLDER = "folder"
    ENTRY = "entry"
    ENTRY_STATE = "entry_state"
    ENTRY_TAG = "entry_tag"
    TAG = "tag"
    RULE = "rule"
    SAVED_SEARCH = "saved_search"


class ChangeOp(BaseModel):
    """Una operación del diario de cambios: un campo de una entidad."""

    device_id: str
    lamport: int
    entity: Entity
    entity_id: str
    field: str
    value: Any = None
    ts: int = Field(default_factory=now_ms)
    seq: int | None = None  # lo asigna el hub al aceptarla

    def wins_over(self, other_lamport: int, other_device: str) -> bool:
        """Last-write-wins determinista: el reloj decide, el device_id desempata."""
        if self.lamport != other_lamport:
            return self.lamport > other_lamport
        return self.device_id > other_device


class SyncScope(BaseModel):
    """Replicación parcial: qué parte del archivo replica un cliente.

    El móvil nunca baja el archivo entero; declara una ventana y el hub filtra
    el pull en el servidor.
    """

    days: int | None = 30  # None = sin límite temporal
    include_starred: bool = True
    include_unread: bool = True
    folder_ids: list[str] = Field(default_factory=list)  # vacío = todas
    feed_ids: list[str] = Field(default_factory=list)
    max_entries: int = 20000


class PullResponse(BaseModel):
    ops: list[ChangeOp] = Field(default_factory=list)
    cursor: int = 0
    has_more: bool = False
    server_lamport: int = 0


class PushRequest(BaseModel):
    device_id: str
    ops: list[ChangeOp] = Field(default_factory=list)


class PushResponse(BaseModel):
    accepted: int = 0
    rejected: int = 0
    cursor: int = 0
    server_lamport: int = 0


# ------------------------------------------------------------------ exportación
class ExportKind(StrEnum):
    OBSIDIAN = "obsidian"
    KINDLE = "kindle"
    MAGAZINE = "magazine"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class ExportJob(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: ExportKind
    status: ExportStatus = ExportStatus.PENDING
    target: str = ""  # 'desktop' para lo que materializa el PC
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: int = Field(default_factory=now_ms)
    started_at: int | None = None
    finished_at: int | None = None


class EntrySelection(BaseModel):
    """Qué artículos entran en una exportación o en una revista."""

    entry_ids: list[str] = Field(default_factory=list)
    feed_ids: list[str] = Field(default_factory=list)
    folder_ids: list[str] = Field(default_factory=list)
    tag_ids: list[str] = Field(default_factory=list)
    query: str | None = None
    unread_only: bool = False
    starred_only: bool = False
    since: int | None = None
    until: int | None = None
    limit: int = 200
    offset: int = 0  # paginación: la lista del escritorio carga por lotes
