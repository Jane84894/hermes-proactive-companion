ALTER TABLE social_decisions ADD COLUMN candidate_title TEXT NOT NULL DEFAULT '';
ALTER TABLE social_decisions ADD COLUMN draft_status TEXT NOT NULL DEFAULT 'not_generated'
    CHECK (draft_status IN ('not_generated', 'generated', 'rejected', 'budget_deferred', 'failed'));
ALTER TABLE social_decisions ADD COLUMN draft_text TEXT;
ALTER TABLE social_decisions ADD COLUMN draft_basis_json TEXT NOT NULL DEFAULT '[]';
