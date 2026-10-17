ALTER TABLE proactive_actions ADD COLUMN delivered_at TEXT;
ALTER TABLE proactive_actions ADD COLUMN responded_at TEXT;

CREATE INDEX IF NOT EXISTS idx_proactive_actions_delivered_at
    ON proactive_actions(state, delivered_at);
