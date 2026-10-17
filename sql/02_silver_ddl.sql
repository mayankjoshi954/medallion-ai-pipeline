-- Silver: typed, validated, deduplicated. Rebuilt in full (TRUNCATE + INSERT) inside one transaction.
CREATE TABLE IF NOT EXISTS silver.tickets (
    ticket_id              TEXT PRIMARY KEY,
    created_at             TIMESTAMP,
    resolved_at            TIMESTAMP,
    category               TEXT,
    category_raw           TEXT,
    category_swap_corrected BOOLEAN NOT NULL DEFAULT FALSE,
    -- agent | baseline (which mapping file matched), agent_swap (agent-detected swap), rule_swap (rules.py swap)
    category_source        TEXT,
    priority               TEXT CHECK (priority IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    status                 TEXT CHECK (status IN ('Open', 'In Progress', 'Pending Vendor', 'Escalated', 'Resolved', 'Closed')),
    building               TEXT,
    description            TEXT,
    submitted_by           TEXT,
    assigned_to            TEXT,
    resolution_notes       TEXT,
    cost                   NUMERIC(12, 2),
    sla_hours              INTEGER,
    -- Anomalies we fixed or nulled but chose to KEEP the row for; see FLAGS in src/silver/rules.py for meanings.
    data_quality_flags     TEXT[] NOT NULL DEFAULT '{}',
    -- lineage back to bronze
    source_row_hash        CHAR(64) NOT NULL,
    bronze_batch_id        TEXT     NOT NULL,
    silver_run_id          TEXT     NOT NULL,
    silver_built_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Added with the auto-applied semantic agent; keeps databases created before it in step.
ALTER TABLE silver.tickets ADD COLUMN IF NOT EXISTS category_source TEXT;

-- Dead-letter queue: rows we refuse to promote. Raw payload is preserved so they can be replayed after a rule fix.
CREATE TABLE IF NOT EXISTS silver.rejected_rows (
    reject_id        BIGSERIAL PRIMARY KEY,
    ticket_id_raw    TEXT,
    reason           TEXT        NOT NULL,
    raw_payload      JSONB       NOT NULL,
    source_row_hash  CHAR(64)    NOT NULL,
    bronze_batch_id  TEXT        NOT NULL,
    silver_run_id    TEXT        NOT NULL,
    rejected_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
