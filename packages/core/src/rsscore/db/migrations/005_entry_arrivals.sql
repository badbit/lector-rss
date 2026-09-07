-- Cursor de altas independiente del diario de cambios de estado. Incluye el
-- archivo existente para que los clientes anteriores puedan ponerse al día.
CREATE TABLE entry_arrivals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE REFERENCES entries(id) ON DELETE CASCADE
);
INSERT INTO entry_arrivals (entry_id) SELECT id FROM entries ORDER BY rowid;
CREATE TRIGGER entries_arrival AFTER INSERT ON entries BEGIN
    INSERT INTO entry_arrivals (entry_id) VALUES (NEW.id);
END;
