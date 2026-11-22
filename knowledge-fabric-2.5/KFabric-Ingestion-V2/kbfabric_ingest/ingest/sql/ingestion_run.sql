CREATE TABLE IF NOT EXISTS ingestion_run (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    ingestion_batch_id TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL,
    auth_source TEXT NOT NULL CHECK (auth_source IN ('kong_jwt', 'dev_test_token')),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'SUBMITTED' CHECK (
        status IN ('SUBMITTED', 'RUNNING', 'SUCCEEDED', 'FAILED')
    ),
    customer_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    industry_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    product_line_id TEXT NOT NULL,
    customer_ontology_id TEXT NOT NULL,
    customer_ontology_version INTEGER NOT NULL,
    canonical_ontology_id TEXT NOT NULL,
    canonical_ontology_version TEXT NOT NULL,
    extraction_policy_version TEXT,
    document_nature TEXT NOT NULL,
    hitl_mode TEXT,
    force_rewrite_requested BOOLEAN NOT NULL DEFAULT FALSE,
    error_message TEXT,
    run_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    submission_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Additive migration for databases created before submission provenance was captured.
ALTER TABLE ingestion_run
    ADD COLUMN IF NOT EXISTS submission_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS source_manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS error_json JSONB,
    ADD COLUMN IF NOT EXISTS auto_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (auto_retry_count >= 0);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_scope_submitted
    ON ingestion_run (customer_id, domain_id, product_line_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_run_submitted_by
    ON ingestion_run (submitted_by, submitted_at DESC);


-- Expand lifecycle states without repeating DDL locks on an already-current schema.
DO $$
DECLARE
    current_definition TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid)
      INTO current_definition
      FROM pg_constraint
     WHERE conrelid = 'ingestion_run'::regclass
       AND conname = 'ingestion_run_status_check';

    IF current_definition IS NOT NULL
       AND current_definition NOT ILIKE '%COMPLETED_WITH_REVIEW_PENDING%' THEN
        ALTER TABLE ingestion_run DROP CONSTRAINT ingestion_run_status_check;
        current_definition := NULL;
    END IF;

    IF current_definition IS NULL THEN
        ALTER TABLE ingestion_run
            ADD CONSTRAINT ingestion_run_status_check CHECK (
                status IN (
                    'SUBMITTED', 'QUEUED', 'VALIDATING', 'RUNNING', 'RETRY_WAIT',
                    'CANCEL_REQUESTED', 'CANCELLED', 'COMPLETED',
                    'COMPLETED_WITH_REVIEW_PENDING', 'FAILED', 'SUCCEEDED'
                )
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS ingestion_run_stage (
    run_id TEXT NOT NULL REFERENCES ingestion_run(run_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'SKIPPED', 'FAILED')
    ),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    skip_reason TEXT,
    progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    checkpoint_id TEXT,
    error_json JSONB,
    PRIMARY KEY (run_id, stage_id)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_stage_status
    ON ingestion_run_stage (run_id, status);

CREATE TABLE IF NOT EXISTS ingestion_run_event (
    event_sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES ingestion_run(run_id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    stage_id TEXT,
    status TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_event_replay
    ON ingestion_run_event (run_id, event_sequence);
CREATE INDEX IF NOT EXISTS idx_ingestion_run_event_job
    ON ingestion_run_event (job_id, event_sequence);

-- Checkpoint records reference separately retained, immutable worker artifacts.
-- A checkpoint row alone never makes a run resumable; the artifact and digests
-- must be verified by the worker restore path first.
CREATE TABLE IF NOT EXISTS ingestion_run_checkpoint (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ingestion_run(run_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    unit_key TEXT NOT NULL,
    artifact_uri TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    source_manifest_sha256 TEXT NOT NULL CHECK (source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    context_sha256 TEXT NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
    cursor_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, stage_id, attempt, unit_key)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_checkpoint_latest
    ON ingestion_run_checkpoint (run_id, created_at DESC);

-- Independent store write ledger. SUCCEEDED units may be skipped during a
-- recovery only when their immutable unit key matches the admission artifact.
CREATE TABLE IF NOT EXISTS ingestion_run_store_write (
    run_id TEXT NOT NULL REFERENCES ingestion_run(run_id) ON DELETE CASCADE,
    store_name TEXT NOT NULL CHECK (store_name IN ('weaviate', 'neo4j')),
    unit_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'UNKNOWN')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_json JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, store_name, unit_key)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_store_write_status
    ON ingestion_run_store_write (run_id, store_name, status);

CREATE TABLE IF NOT EXISTS ingestion_run_attempt (
    run_id TEXT NOT NULL REFERENCES ingestion_run(run_id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL CHECK (attempt >= 2),
    idempotency_key TEXT NOT NULL,
    ray_submission_id TEXT NOT NULL UNIQUE,
    requested_by TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (
        status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')
    ),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, attempt),
    UNIQUE (run_id, idempotency_key)
);
