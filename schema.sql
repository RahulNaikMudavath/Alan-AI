-- RecallKit database schema.
-- Statement is idempotent: init_db() runs it on every application startup.

CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    agent_id   TEXT,
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'fact',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(content)) > 0),
    CHECK (length(user_id) > 0)
);

-- Primary read pattern: "this user's memories, newest first".
CREATE INDEX IF NOT EXISTS idx_memories_user_created
    ON memories (user_id, created_at DESC);

-- Secondary read pattern: narrowing to one agent inside a user.
CREATE INDEX IF NOT EXISTS idx_memories_user_agent
    ON memories (user_id, agent_id);

-- Full-text index for keyword search.
--
-- This is a *standalone* FTS5 table rather than an external-content one: it
-- keeps its own copy of the text, which costs some disk space but means the
-- schema needs no `content=` bookkeeping and every write path stays obvious.
-- `memory_id` is UNINDEXED because it is only ever used for joining back to
-- `memories`, never searched.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Keep the index in step with the table. Doing this with triggers (rather than
-- in the repository) means the index cannot drift, even if a row is changed by
-- hand with sqlite3 while debugging.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (memory_id, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
    INSERT INTO memories_fts (memory_id, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;

-- Backfill: covers databases created before the index existed. This is a no-op
-- once every row is indexed, and it is what makes adding FTS to a live database
-- safe.
INSERT INTO memories_fts (memory_id, content)
SELECT m.id, m.content
FROM memories m
WHERE NOT EXISTS (SELECT 1 FROM memories_fts f WHERE f.memory_id = m.id);

-- Semantic-search vectors.
--
-- One row per (memory, model). Keying by model means changing embedding models
-- is additive: old vectors are ignored rather than mixed with the new ones,
-- and re-embedding can happen incrementally.
--
-- `vector` holds little-endian float32 values packed by app/retrieval/vectors.py.
-- The FK cascades so deleting a memory also deletes its vectors; that requires
-- `PRAGMA foreign_keys = ON`, which Database.connect() sets on every connection.
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, model),
    CHECK (dimensions > 0),
    CHECK (length(vector) = dimensions * 4)
);

-- Supports "all vectors for this model" scans without touching other models.
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
    ON memory_embeddings (model);