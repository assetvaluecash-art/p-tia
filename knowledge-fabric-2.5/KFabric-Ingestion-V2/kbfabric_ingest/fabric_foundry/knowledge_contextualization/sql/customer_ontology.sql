CREATE TABLE IF NOT EXISTS customer_ontology (
    customer_ontology_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    ontology_id TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    customer_ontology_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN ('ACTIVE', 'INACTIVE')
    ),
    definition JSONB NOT NULL DEFAULT '{}'::jsonb,

    change_type TEXT NOT NULL DEFAULT 'INITIAL',
    change_reason TEXT,
    request_id TEXT,
    change_summary JSONB NOT NULL DEFAULT '{}'::jsonb,

    parent_customer_ontology_id UUID,

    created_by TEXT NOT NULL DEFAULT 'SYSTEM',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    confirmed_by TEXT,
    confirmed_at TIMESTAMPTZ,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_customer_ontology_customer
        FOREIGN KEY (tenant_id)
        REFERENCES customer(tenant_id),

    CONSTRAINT fk_customer_ontology_canonical
        FOREIGN KEY (ontology_id, ontology_version)
        REFERENCES ontology(ontology_id, version),

    CONSTRAINT fk_customer_ontology_parent
        FOREIGN KEY (parent_customer_ontology_id)
        REFERENCES customer_ontology(customer_ontology_id),

    CONSTRAINT uq_customer_ontology_version
        UNIQUE (tenant_id, customer_ontology_version),

    CONSTRAINT uq_customer_ontology_request
        UNIQUE (tenant_id, request_id),

    CONSTRAINT customer_ontology_version_positive
        CHECK (customer_ontology_version > 0),

    CONSTRAINT customer_ontology_change_type_check
        CHECK (
            change_type IN (
                'INITIAL',
                'CONTEXTUALIZATION',
                'ACTIVATION',
                'DEACTIVATION',
                'ALIAS',
                'ADDITION',
                'MIXED'
            )
        )
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'change_type'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN change_type TEXT NOT NULL DEFAULT 'INITIAL';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'change_reason'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN change_reason TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'request_id'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN request_id TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'change_summary'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN change_summary JSONB NOT NULL DEFAULT '{}'::jsonb;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'parent_customer_ontology_id'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN parent_customer_ontology_id UUID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'created_by'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN created_by TEXT NOT NULL DEFAULT 'SYSTEM';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'confirmed_by'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN confirmed_by TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer_ontology'
          AND column_name = 'confirmed_at'
    ) THEN
        ALTER TABLE customer_ontology
            ADD COLUMN confirmed_at TIMESTAMPTZ;
    END IF;
END
$$;

UPDATE customer_ontology
SET
    change_type = COALESCE(change_type, 'INITIAL'),
    change_summary = COALESCE(
        change_summary,
        '{"description": "Initial customer ontology created from canonical ontology"}'::jsonb
    ),
    created_by = COALESCE(created_by, 'SYSTEM')
WHERE customer_ontology_version = 1;

ALTER TABLE customer_ontology
    ALTER COLUMN change_type SET NOT NULL,
    ALTER COLUMN change_summary SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_customer_ontology_parent'
    ) THEN
        ALTER TABLE customer_ontology
            ADD CONSTRAINT fk_customer_ontology_parent
            FOREIGN KEY (parent_customer_ontology_id)
            REFERENCES customer_ontology(customer_ontology_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_customer_ontology_request'
    ) THEN
        ALTER TABLE customer_ontology
            ADD CONSTRAINT uq_customer_ontology_request
            UNIQUE (tenant_id, request_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'customer_ontology_version_positive'
    ) THEN
        ALTER TABLE customer_ontology
            ADD CONSTRAINT customer_ontology_version_positive
            CHECK (customer_ontology_version > 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'customer_ontology_change_type_check'
    ) THEN
        ALTER TABLE customer_ontology
            ADD CONSTRAINT customer_ontology_change_type_check
            CHECK (
                change_type IN (
                    'INITIAL',
                    'CONTEXTUALIZATION',
                    'ACTIVATION',
                    'DEACTIVATION',
                    'ALIAS',
                    'ADDITION',
                    'MIXED'
                )
            );
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS
    uq_customer_ontology_active
ON customer_ontology (tenant_id)
WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS
    idx_customer_ontology_customer
ON customer_ontology (customer_id);

CREATE INDEX IF NOT EXISTS
    idx_customer_ontology_canonical
ON customer_ontology (ontology_id, ontology_version);

CREATE INDEX IF NOT EXISTS
    idx_customer_ontology_status
ON customer_ontology (status);

CREATE INDEX IF NOT EXISTS
    idx_customer_ontology_parent
ON customer_ontology (parent_customer_ontology_id);

CREATE INDEX IF NOT EXISTS
    idx_customer_ontology_created_at
ON customer_ontology (tenant_id, created_at DESC);
