CREATE TABLE IF NOT EXISTS discoveries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    topic_id TEXT REFERENCES topics(id),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    interest_score REAL NOT NULL CHECK (interest_score BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'delivered', 'responded', 'ignored', 'dismissed', 'expired')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_discoveries_queue
    ON discoveries(status, expires_at, interest_score DESC);

CREATE TABLE IF NOT EXISTS worker_runs (
    worker_name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    last_error_code TEXT
);
