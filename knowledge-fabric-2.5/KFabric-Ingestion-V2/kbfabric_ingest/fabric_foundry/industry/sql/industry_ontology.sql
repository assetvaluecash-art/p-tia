CREATE TABLE IF NOT EXISTS industry_ontology (
    industry_id TEXT NOT NULL,

    ontology_id TEXT NOT NULL,
    ontology_version TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN (
            'ACTIVE',
            'INACTIVE'
        )
    ),

    is_default BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (
        industry_id,
        ontology_id,
        ontology_version
    ),

    CONSTRAINT fk_industry_ontology_industry
        FOREIGN KEY (industry_id)
        REFERENCES industry (industry_id),

    CONSTRAINT fk_industry_ontology_ontology
        FOREIGN KEY (ontology_id, ontology_version)
        REFERENCES ontology (ontology_id, version)
);

CREATE INDEX IF NOT EXISTS idx_industry_ontology_industry
    ON industry_ontology (industry_id);

CREATE INDEX IF NOT EXISTS idx_industry_ontology_ontology
    ON industry_ontology (ontology_id, ontology_version);

CREATE INDEX IF NOT EXISTS idx_industry_ontology_status
    ON industry_ontology (status);
