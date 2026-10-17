-- Run-level observability. Queryable with plain SQL; no external tooling needed.
CREATE TABLE IF NOT EXISTS lineage.pipeline_runs (
    id             BIGSERIAL PRIMARY KEY,
    run_id         TEXT        NOT NULL,
    stage          TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ,
    duration_ms    INTEGER,
    rows_in        INTEGER,
    rows_out       INTEGER,
    rows_rejected  INTEGER,
    details        JSONB,
    error_message  TEXT
);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_stage_started ON lineage.pipeline_runs (stage, started_at DESC);

-- One row per LLM call, including cache hits (cost 0), so spend and call counts are auditable.
CREATE TABLE IF NOT EXISTS lineage.agent_runs (
    id                 BIGSERIAL PRIMARY KEY,
    agent_name         TEXT        NOT NULL,
    model              TEXT        NOT NULL,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    estimated_cost_usd NUMERIC(10, 6),
    duration_ms        INTEGER,
    cache_hit          BOOLEAN     NOT NULL,
    status             TEXT        NOT NULL,
    output_file        TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lineage.alerts (
    alert_id         BIGSERIAL PRIMARY KEY,
    run_id           TEXT        NOT NULL,
    stage            TEXT        NOT NULL,
    severity         TEXT        NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
    metric_name      TEXT        NOT NULL,
    metric_value     NUMERIC,
    message          TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at      TIMESTAMPTZ,
    notified_channel TEXT
);

-- One row per Great Expectations expectation per run, so data quality is trendable with plain SQL.
CREATE TABLE IF NOT EXISTS lineage.quality_results (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             TEXT        NOT NULL,
    suite              TEXT        NOT NULL,
    expectation        TEXT        NOT NULL,
    column_name        TEXT,
    severity           TEXT        NOT NULL,
    success            BOOLEAN     NOT NULL,
    element_count      INTEGER,
    unexpected_count   INTEGER,
    unexpected_percent NUMERIC(6, 2),
    observed_value     TEXT,
    detail             TEXT,
    why                TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "How many are in the DLQ, and why?"
CREATE OR REPLACE VIEW lineage.v_dlq_summary AS
SELECT reason,
       COUNT(*)                                             AS rejected_rows,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)   AS pct_of_rejected
FROM silver.rejected_rows
GROUP BY reason;

-- "Which cleaning rules fired, and how often?"
CREATE OR REPLACE VIEW lineage.v_quality_flag_summary AS
SELECT flag,
       COUNT(*)                                                        AS tickets,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM silver.tickets), 2) AS pct_of_silver
FROM silver.tickets, UNNEST(data_quality_flags) AS flag
GROUP BY flag;
