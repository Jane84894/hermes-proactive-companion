CREATE TABLE IF NOT EXISTS thought_queue (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    thought_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    possible_action TEXT NOT NULL DEFAULT '',
    priority REAL NOT NULL DEFAULT 0.5 CHECK (priority BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    earliest_send_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'candidate', 'pending', 'ready', 'selected', 'delivered', 'engaged',
            'ignored', 'completed', 'cancelled', 'expired', 'merged', 'failed'
        )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_evaluated_at TEXT,
    related_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    source_key TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_thought_queue_ready
    ON thought_queue(user_id, status, earliest_send_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_thought_queue_created
    ON thought_queue(user_id, created_at);
