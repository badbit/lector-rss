-- Procedencia local: se conserva el registro original, incluso si se retira
-- después el artículo. Los cuerpos se distribuyen desde el hub bajo demanda.
CREATE TABLE import_batches (
    sha256 TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    filename TEXT NOT NULL,
    imported_at INTEGER NOT NULL,
    subscriptions_xml TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE import_records (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    entry_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL,
    imported_at INTEGER NOT NULL,
    PRIMARY KEY (source, external_id)
);
