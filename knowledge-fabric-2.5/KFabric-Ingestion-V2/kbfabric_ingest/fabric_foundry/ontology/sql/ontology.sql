CREATE TABLE IF NOT EXISTS ontology (
    ontology_id TEXT NOT NULL,
    ontology_name TEXT NOT NULL,
    version TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN (
            'ACTIVE',
            'INACTIVE'
        )
    ),

    artifact_path TEXT NOT NULL,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (
        ontology_id,
        version
    )
);

CREATE INDEX IF NOT EXISTS idx_ontology_status
    ON ontology (status);

CREATE INDEX IF NOT EXISTS idx_ontology_id
    ON ontology (ontology_id);
