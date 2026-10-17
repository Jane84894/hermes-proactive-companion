CREATE TABLE IF NOT EXISTS topic_sources (
    topic_id TEXT NOT NULL REFERENCES topics(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    source_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(topic_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_sources_source
    ON topic_sources(user_id, source_message_id);
