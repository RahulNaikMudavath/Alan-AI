-- M3.3: memory lifecycle and conflict candidates.
--
-- Applied exactly once by the migration runner, inside a single transaction.
-- Do not add BEGIN/COMMIT here; the runner owns the transaction.

-- Lifecycle columns. Existing rows become active, which is the only state
-- that existed before this migration.
ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'superseded'));

ALTER TABLE memories ADD COLUMN superseded_at TEXT;

-- Deleting the replacement must not block deletion of a memory. The pointer is
-- cleared, while the timestamp and reason stay as history.
ALTER TABLE memories ADD COLUMN superseded_by TEXT
    REFERENCES memories(id) ON DELETE SET NULL;

ALTER TABLE memories ADD COLUMN superseded_reason TEXT
    CHECK (
        superseded_reason IS NULL
        OR superseded_reason IN ('user_correction', 'outdated', 'contradiction', 'duplicate', 'other')
    );

ALTER TABLE memories ADD COLUMN superseded_note TEXT;

-- Candidate contradictions. These are heuristic and never change memory state
-- on their own; a caller must resolve them explicitly.
CREATE TABLE IF NOT EXISTS memory_conflicts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    memory_a    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    memory_b    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved')),
    rule        TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    resolution  TEXT
                CHECK (
                    resolution IS NULL
                    OR resolution IN ('kept_a', 'kept_b', 'kept_both', 'superseded')
                ),
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    -- One row per unordered pair: the pair is stored in canonical order.
    CHECK (memory_a < memory_b),
    CHECK (
        (status = 'open' AND resolution IS NULL AND resolved_at IS NULL)
        OR
        (status = 'resolved' AND resolution IS NOT NULL AND resolved_at IS NOT NULL)
    ),
    UNIQUE (user_id, memory_a, memory_b)
);

CREATE INDEX IF NOT EXISTS idx_memories_user_status_created
    ON memories (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_conflicts_user_status
    ON memory_conflicts (user_id, status);

CREATE INDEX IF NOT EXISTS idx_memory_conflicts_memory_a
    ON memory_conflicts (memory_a);

CREATE INDEX IF NOT EXISTS idx_memory_conflicts_memory_b
    ON memory_conflicts (memory_b);