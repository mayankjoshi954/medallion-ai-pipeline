-- Bronze: source columns kept as TEXT exactly as received (schema-on-read), plus lineage columns.
CREATE TABLE IF NOT EXISTS bronze.raw_tickets (
    bronze_id          BIGSERIAL PRIMARY KEY,
    ticket_id          TEXT,
    created_at         TEXT,
    resolved_at        TEXT,
    category           TEXT,
    priority           TEXT,
    status             TEXT,
    building           TEXT,
    description        TEXT,
    submitted_by       TEXT,
    assigned_to        TEXT,
    resolution_notes   TEXT,
    cost               TEXT,
    sla_hours          TEXT,
    -- columns the source added that we don't model yet: kept here so drift never loses data
    extra_columns      JSONB,
    -- lineage
    source_file        TEXT        NOT NULL,
    source_row_number  INTEGER     NOT NULL,
    batch_id           TEXT        NOT NULL,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- sha256 over the 13 source columns (+ extra_columns if any); UNIQUE makes re-ingesting the same content a no-op
    row_hash           CHAR(64)    NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS ix_bronze_raw_tickets_ticket_id ON bronze.raw_tickets (ticket_id);

-- One row per file load: the direct answer to "how many rows did we get".
CREATE TABLE IF NOT EXISTS bronze.ingestion_log (
    ingestion_id           BIGSERIAL PRIMARY KEY,
    batch_id               TEXT        NOT NULL,
    source_file            TEXT        NOT NULL,
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_rows_in_file     INTEGER     NOT NULL,
    rows_inserted_new      INTEGER     NOT NULL,
    rows_skipped_duplicate INTEGER     NOT NULL,
    status                 TEXT        NOT NULL,
    duration_ms            INTEGER     NOT NULL
);
