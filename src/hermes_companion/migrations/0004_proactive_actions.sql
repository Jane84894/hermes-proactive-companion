CREATE TABLE IF NOT EXISTS proactive_actions (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'candidate'
        CHECK (state IN (
            'candidate', 'draft_ready', 'approved', 'generating', 'ready', 'sending',
            'delivered', 'generation_failed', 'send_failed', 'delivery_unknown',
            'cancelled', 'expired', 'responded', 'ignored', 'closed'
        )),
    payload_json TEXT NOT NULL DEFAULT '{}',
    draft_text TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    delivery_receipt TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_proactive_actions_state
    ON proactive_actions(state, updated_at);
