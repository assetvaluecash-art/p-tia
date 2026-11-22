CREATE TABLE IF NOT EXISTS hitl_review_event (
    event_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES hitl_review(review_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('REVIEW_DECISION', 'WRITE_EXECUTION')),
    action TEXT NOT NULL,
    status_before TEXT NOT NULL,
    status_after TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewer_notes TEXT,
    resolved_plan_json JSONB,
    event_details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hitl_review_event_review_created
    ON hitl_review_event (review_id, created_at, event_id);

ALTER TABLE hitl_review_event
    DROP CONSTRAINT IF EXISTS hitl_review_event_event_type_check;
ALTER TABLE hitl_review_event
    ADD CONSTRAINT hitl_review_event_event_type_check
    CHECK (event_type IN ('REVIEW_DECISION', 'WRITE_EXECUTION'));
ALTER TABLE hitl_review_event
    ADD COLUMN IF NOT EXISTS event_details_json JSONB NOT NULL DEFAULT '{}'::jsonb;
