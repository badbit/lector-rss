-- Esquema inicial del lector RSS.
-- Diseñado para archivo permanente (500+ feeds, millones de entradas):
--   * metadatos y cuerpos en tablas separadas, para que listar no toque páginas grandes
--   * estado de lectura aislado, para que marcar como leído no reescriba metadatos
--   * FTS5 contentless: índice sin duplicar el texto (los fragmentos se generan al vuelo)

-- ---------------------------------------------------------------- jerarquía
CREATE TABLE folders (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES folders(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    lamport     INTEGER NOT NULL DEFAULT 0,
    device_id   TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_folders_parent ON folders(parent_id) WHERE deleted = 0;

CREATE TABLE feeds (
    id              TEXT PRIMARY KEY,
    folder_id       TEXT REFERENCES folders(id) ON DELETE SET NULL,
    url             TEXT NOT NULL UNIQUE,
    site_url        TEXT,
    title           TEXT NOT NULL DEFAULT '',
    custom_title    TEXT,
    description     TEXT,
    icon_url        TEXT,
    -- contabilidad de descarga (local al hub, no se sincroniza)
    etag            TEXT,
    last_modified   TEXT,
    last_fetch_at   INTEGER,
    last_success_at INTEGER,
    next_fetch_at   INTEGER NOT NULL DEFAULT 0,
    interval_seconds INTEGER NOT NULL DEFAULT 1800,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    disabled        INTEGER NOT NULL DEFAULT 0,
    -- política de contenido
    fetch_full_text INTEGER NOT NULL DEFAULT 0,
    keep_unread     INTEGER NOT NULL DEFAULT 0,
    -- sincronización
    deleted         INTEGER NOT NULL DEFAULT 0,
    lamport         INTEGER NOT NULL DEFAULT 0,
    device_id       TEXT NOT NULL DEFAULT '',
    updated_at      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_feeds_folder ON feeds(folder_id) WHERE deleted = 0;
CREATE INDEX idx_feeds_due ON feeds(next_fetch_at) WHERE disabled = 0 AND deleted = 0;

-- ---------------------------------------------------------------- entradas
CREATE TABLE entries (
    id            TEXT PRIMARY KEY,
    feed_id       TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid_hash     TEXT NOT NULL,      -- sha256(feed_id + guid|link): identidad estable
    content_hash  TEXT NOT NULL,      -- sha256(texto normalizado): detecta ediciones y duplicados
    url           TEXT,
    title         TEXT NOT NULL DEFAULT '',
    author        TEXT,
    summary       TEXT,               -- resumen corto para la lista
    published_at  INTEGER NOT NULL,
    updated_at    INTEGER,
    fetched_at    INTEGER NOT NULL,
    has_body      INTEGER NOT NULL DEFAULT 0,
    full_text_at  INTEGER,            -- cuándo se extrajo el texto completo, NULL si no
    enclosure_url  TEXT,
    enclosure_type TEXT,
    UNIQUE (feed_id, guid_hash)
);
CREATE INDEX idx_entries_feed_pub ON entries(feed_id, published_at DESC);
CREATE INDEX idx_entries_pub ON entries(published_at DESC);
CREATE INDEX idx_entries_content_hash ON entries(content_hash);

-- Cuerpos: tabla aparte y comprimidos con zstd (~3-4x en texto).
CREATE TABLE entry_bodies (
    entry_id  TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    html_zstd BLOB,
    text_zstd BLOB,
    bytes_raw INTEGER NOT NULL DEFAULT 0
);

-- Estado de lectura: se sincroniza, se escribe muy a menudo.
CREATE TABLE entry_state (
    entry_id  TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    read      INTEGER NOT NULL DEFAULT 0,
    starred   INTEGER NOT NULL DEFAULT 0,
    read_at   INTEGER,
    star_at   INTEGER,
    lamport   INTEGER NOT NULL DEFAULT 0,
    device_id TEXT NOT NULL DEFAULT ''
);
-- Índice parcial: "no leídos" no debe escanear el archivo histórico completo.
CREATE INDEX idx_state_unread ON entry_state(entry_id) WHERE read = 0;
CREATE INDEX idx_state_starred ON entry_state(entry_id) WHERE starred = 1;

-- ---------------------------------------------------------------- etiquetas
CREATE TABLE tags (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    color     TEXT,
    deleted   INTEGER NOT NULL DEFAULT 0,
    lamport   INTEGER NOT NULL DEFAULT 0,
    device_id TEXT NOT NULL DEFAULT ''
);

-- LWW-element-set: el tombstone evita que quitar una etiqueta se pierda
-- frente a una escritura concurrente en otro dispositivo.
CREATE TABLE entry_tags (
    entry_id  TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    tag_id    TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    deleted   INTEGER NOT NULL DEFAULT 0,
    lamport   INTEGER NOT NULL DEFAULT 0,
    device_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (entry_id, tag_id)
);
CREATE INDEX idx_entry_tags_tag ON entry_tags(tag_id) WHERE deleted = 0;

-- ---------------------------------------------------------------- búsqueda
-- contentless: guarda el índice pero no el texto (ya está comprimido en entry_bodies).
CREATE VIRTUAL TABLE entries_fts USING fts5(
    title, author, body,
    content = '',
    contentless_delete = 1,
    tokenize = "unicode61 remove_diacritics 2"
);

-- ---------------------------------------------------------------- reglas
CREATE TABLE rules (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    position   INTEGER NOT NULL DEFAULT 0,
    spec_json  TEXT NOT NULL,        -- la regla completa validada por Pydantic
    deleted    INTEGER NOT NULL DEFAULT 0,
    lamport    INTEGER NOT NULL DEFAULT 0,
    device_id  TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE saved_searches (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    query      TEXT NOT NULL,        -- expresión FTS5 + filtros
    filter_json TEXT NOT NULL DEFAULT '{}',
    position   INTEGER NOT NULL DEFAULT 0,
    deleted    INTEGER NOT NULL DEFAULT 0,
    lamport    INTEGER NOT NULL DEFAULT 0,
    device_id  TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------- sincronización
-- Diario de cambios: es lo que consumen los clientes en /sync/pull.
CREATE TABLE change_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    lamport    INTEGER NOT NULL,
    entity     TEXT NOT NULL,        -- feed | folder | entry_state | entry_tag | tag | rule | saved_search
    entity_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_json TEXT NOT NULL,
    ts         INTEGER NOT NULL
);
CREATE INDEX idx_change_log_entity ON change_log(entity, entity_id, field);

-- Operaciones locales pendientes de subir (solo se usa en los clientes).
CREATE TABLE outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    lamport    INTEGER NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_json TEXT NOT NULL,
    ts         INTEGER NOT NULL
);

-- Dispositivos conocidos y hasta dónde ha leído cada uno (solo en el hub).
CREATE TABLE sync_clients (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    last_seq    INTEGER NOT NULL DEFAULT 0,
    scope_json  TEXT NOT NULL DEFAULT '{}',   -- replicación parcial del móvil
    last_seen_at INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- exportación
CREATE TABLE export_jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,       -- obsidian | kindle | magazine
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error
    target       TEXT NOT NULL DEFAULT '',         -- p.ej. 'desktop' para lo que materializa el PC
    params_json  TEXT NOT NULL DEFAULT '{}',
    result_json  TEXT NOT NULL DEFAULT '{}',
    error        TEXT,
    created_at   INTEGER NOT NULL,
    started_at   INTEGER,
    finished_at  INTEGER
);
CREATE INDEX idx_export_jobs_pending ON export_jobs(target, created_at) WHERE status = 'pending';

-- ---------------------------------------------------------------- varios
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Reloj Lamport y estado local del nodo.
CREATE TABLE node (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    device_id TEXT NOT NULL,
    lamport   INTEGER NOT NULL DEFAULT 0,
    last_pull_seq INTEGER NOT NULL DEFAULT 0
);
