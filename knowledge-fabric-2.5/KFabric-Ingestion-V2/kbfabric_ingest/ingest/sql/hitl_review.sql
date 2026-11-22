CREATE TABLE IF NOT EXISTS hitl_review (
    review_id TEXT PRIMARY KEY,
    ingestion_batch_id TEXT,
    plan_id TEXT NOT NULL UNIQUE,

    queue_type TEXT NOT NULL CHECK (
        queue_type IN (
            'NODE_ADMISSION',
            'CONTEXT_RESOLUTION',
            'UNRESOLVED_TARGET'
        )
    ),

    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED')
    ),

    priority TEXT NOT NULL DEFAULT 'NORMAL' CHECK (
        priority IN ('LOW', 'NORMAL', 'HIGH', 'CRITICAL')
    ),

    reason_code TEXT NOT NULL,

    admission_behavior TEXT NOT NULL CHECK (
        admission_behavior IN (
            'WAIT_FOR_NODE_DECISION',
            'NODE_CAN_EXIST'
        )
    ),

    short_summary TEXT,
    decision_question TEXT,

    source_temporary_id TEXT,

    source_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    document_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ontology_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    mapping_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_resolution_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    relationship_evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    provenance_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    review_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    execution_plan_json JSONB NOT NULL,
    resolved_plan_json JSONB,

    reviewer_action TEXT,
    reviewer_id TEXT,
    reviewer_notes TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_hitl_review_ingestion_batch
    ON hitl_review (ingestion_batch_id);

CREATE INDEX IF NOT EXISTS idx_hitl_review_batch_status
    ON hitl_review (ingestion_batch_id, status);

CREATE INDEX IF NOT EXISTS idx_hitl_review_queue_status
    ON hitl_review (queue_type, status);

CREATE INDEX IF NOT EXISTS idx_hitl_review_status
    ON hitl_review (status);

CREATE INDEX IF NOT EXISTS idx_hitl_review_priority_created
    ON hitl_review (priority, created_at);

CREATE INDEX IF NOT EXISTS idx_hitl_review_created
    ON hitl_review (created_at);

CREATE INDEX IF NOT EXISTS idx_hitl_review_source_temporary
    ON hitl_review (source_temporary_id);

ALTER TABLE hitl_review
    ADD COLUMN IF NOT EXISTS write_status TEXT NOT NULL DEFAULT 'NOT_STARTED';
ALTER TABLE hitl_review
    ADD COLUMN IF NOT EXISTS write_result_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE hitl_review
    ADD COLUMN IF NOT EXISTS write_error TEXT;
ALTER TABLE hitl_review
    ADD COLUMN IF NOT EXISTS write_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hitl_review
    ADD COLUMN IF NOT EXISTS write_started_at TIMESTAMPTZ;
