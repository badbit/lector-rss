-- Reloj por CAMPO, no por fila.
--
-- El last-write-wins tiene que decidirse campo a campo: si «leído» y «guardado»
-- comparten un único reloj en la fila de estado, aplicar las dos operaciones en
-- distinto orden da resultados distintos y los dispositivos dejan de converger.
--
-- Solo ocupa espacio por lo que se ha llegado a modificar: un artículo que nadie
-- ha tocado no tiene ninguna fila aquí.
CREATE TABLE field_clock (
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field     TEXT NOT NULL,
    lamport   INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    PRIMARY KEY (entity, entity_id, field)
) WITHOUT ROWID;
