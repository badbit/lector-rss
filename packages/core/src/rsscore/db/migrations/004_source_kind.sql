-- Fuentes que no son un feed RSS.
--
-- `source_kind` decide cómo se convierten los bytes descargados en entradas:
--   feed   → RSS/Atom, lo de siempre
--   scrape → se extraen las entradas de la página con selectores CSS
--   watch  → se vigila una zona de la página y cada cambio genera una entrada
--
-- Todo lo demás (dedup, FTS5, reglas, sincronización, exportación) trata esas
-- entradas exactamente igual que las de un feed.
ALTER TABLE feeds ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'feed';
ALTER TABLE feeds ADD COLUMN source_config_json TEXT NOT NULL DEFAULT '{}';
-- Última huella vista en modo `watch`. Local a cada nodo, como `etag`.
ALTER TABLE feeds ADD COLUMN watch_hash TEXT;
