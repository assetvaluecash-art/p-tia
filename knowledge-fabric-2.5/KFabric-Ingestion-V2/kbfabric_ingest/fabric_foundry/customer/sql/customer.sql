CREATE TABLE IF NOT EXISTS customer (
    tenant_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL UNIQUE,
    customer_name TEXT NOT NULL,
    industry_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN ('ACTIVE','SUSPENDED','DEACTIVATED')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT NOT NULL DEFAULT 'SYSTEM',
    updated_by TEXT NOT NULL DEFAULT 'SYSTEM',
    CONSTRAINT fk_customer_industry
        FOREIGN KEY (industry_id)
        REFERENCES industry(industry_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer'
          AND column_name = 'created_by'
    ) THEN
        ALTER TABLE customer
            ADD COLUMN created_by TEXT NOT NULL DEFAULT 'SYSTEM';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer'
          AND column_name = 'updated_by'
    ) THEN
        ALTER TABLE customer
            ADD COLUMN updated_by TEXT NOT NULL DEFAULT 'SYSTEM';
    END IF;
END
$$;

-- Migrate the previously-created customer table from free-text
-- industry to the Fabric Foundry industry registry.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer'
          AND column_name = 'industry'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'customer'
          AND column_name = 'industry_id'
    ) THEN
        -- The existing customer table is known to contain zero rows.
        DROP INDEX IF EXISTS idx_customer_industry;

        ALTER TABLE customer
            DROP COLUMN industry;

        ALTER TABLE customer
            ADD COLUMN industry_id TEXT;

        ALTER TABLE customer
            ALTER COLUMN industry_id SET NOT NULL;

        ALTER TABLE customer
            ADD CONSTRAINT fk_customer_industry
            FOREIGN KEY (industry_id)
            REFERENCES industry(industry_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_customer_customer_id
    ON customer(customer_id);

CREATE INDEX IF NOT EXISTS idx_customer_industry_id
    ON customer(industry_id);

CREATE INDEX IF NOT EXISTS idx_customer_status
    ON customer(status);
