CREATE TABLE IF NOT EXISTS social_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_slot TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_kind TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    score REAL,
    reason_codes_json TEXT NOT NULL,
    components_json TEXT NOT NULL,
    UNIQUE(evaluation_slot, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_social_decisions_evaluated
    ON social_decisions(evaluated_at, selected);
