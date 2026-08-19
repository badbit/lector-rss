-- Operaciones que llegan antes que la entidad a la que se refieren.
--
-- Pasa constantemente en la práctica: el móvil marca como leído un artículo que
-- el escritorio todavía no ha descargado, o al revés. Descartar esas operaciones
-- perdería el cambio para siempre, así que se aparcan y se reaplican cuando la
-- entidad aparece.
CREATE TABLE sync_pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_json TEXT NOT NULL,
    lamport    INTEGER NOT NULL,
    device_id  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    tries      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (entity, entity_id, field, device_id, lamport)
);
CREATE INDEX idx_sync_pending_entity ON sync_pending(entity, entity_id);
