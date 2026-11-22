CREATE TABLE IF NOT EXISTS industry (
    industry_id TEXT PRIMARY KEY,

    industry_name TEXT NOT NULL,
    description TEXT,

    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN (
            'ACTIVE',
            'INACTIVE'
        )
    ),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_industry_status
    ON industry (status);

CREATE INDEX IF NOT EXISTS idx_industry_name
    ON industry (industry_name);
