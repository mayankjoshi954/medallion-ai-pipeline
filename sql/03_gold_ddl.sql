-- Gold: business-ready aggregates, fully rebuilt from silver on each run (deterministic, no double counting).

-- Q: where do we miss SLAs, and how long does each kind of work take?
CREATE TABLE IF NOT EXISTS gold.category_sla_performance (
    category                TEXT NOT NULL,
    priority                TEXT NOT NULL,
    ticket_count            INTEGER NOT NULL,
    resolved_count          INTEGER NOT NULL,
    timed_resolution_count  INTEGER NOT NULL,   -- resolved with a trustworthy created/resolved pair
    avg_resolution_hours    NUMERIC(12, 2),
    p90_resolution_hours    NUMERIC(12, 2),
    sla_evaluated_count     INTEGER NOT NULL,   -- timed AND has a valid sla_hours
    sla_breached_count      INTEGER NOT NULL,
    sla_breach_rate_pct     NUMERIC(5, 2),
    gold_run_id             TEXT NOT NULL,
    gold_built_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (category, priority)
);

-- Q: who is doing the work, what does it cost, and how often do tickets escalate?
CREATE TABLE IF NOT EXISTS gold.assignee_performance (
    assigned_to           TEXT PRIMARY KEY,
    assignee_type         TEXT NOT NULL,        -- IN_HOUSE | EXTERNAL_VENDOR | UNASSIGNED
    ticket_count          INTEGER NOT NULL,
    resolved_count        INTEGER NOT NULL,
    escalated_count       INTEGER NOT NULL,
    escalation_rate_pct   NUMERIC(5, 2),
    costed_ticket_count   INTEGER NOT NULL,
    total_cost            NUMERIC(14, 2),
    avg_cost              NUMERIC(12, 2),
    avg_resolution_hours  NUMERIC(12, 2),
    gold_run_id           TEXT NOT NULL,
    gold_built_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Q: what is stuck open, where, and for how long? Age is measured against the newest ticket in the data,
-- not now(), so the table is reproducible.
CREATE TABLE IF NOT EXISTS gold.open_ticket_backlog (
    building       TEXT NOT NULL,
    priority       TEXT NOT NULL,
    age_bucket     TEXT NOT NULL,
    open_count     INTEGER NOT NULL,
    avg_age_days   NUMERIC(10, 1),
    snapshot_date  DATE NOT NULL,
    gold_run_id    TEXT NOT NULL,
    gold_built_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (building, priority, age_bucket)
);
