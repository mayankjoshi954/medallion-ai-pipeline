"""Gold build: three business-facing aggregates, fully rebuilt from silver so reruns never double count."""
import logging

from src.db import transaction
from src.lineage import StageRecord

log = logging.getLogger(__name__)

# "Resolved with a trustworthy time pair": done status and resolved_at not before created_at.
_RESOLUTION_HOURS = """
    CASE WHEN status IN ('Resolved', 'Closed') AND resolved_at IS NOT NULL AND resolved_at >= created_at
         THEN EXTRACT(EPOCH FROM (resolved_at - created_at)) / 3600.0 END
"""

_CATEGORY_SLA = f"""
INSERT INTO gold.category_sla_performance
WITH base AS (
    SELECT COALESCE(category, 'Unclassified') AS category, COALESCE(priority, 'UNSET') AS priority,
           status, sla_hours, {_RESOLUTION_HOURS} AS resolution_hours
    FROM silver.tickets
)
SELECT category, priority,
       COUNT(*),
       COUNT(*) FILTER (WHERE status IN ('Resolved', 'Closed')),
       COUNT(resolution_hours),
       ROUND(AVG(resolution_hours)::numeric, 2),
       ROUND((PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY resolution_hours))::numeric, 2),
       COUNT(*) FILTER (WHERE resolution_hours IS NOT NULL AND sla_hours IS NOT NULL),
       COUNT(*) FILTER (WHERE resolution_hours > sla_hours),
       ROUND(100.0 * COUNT(*) FILTER (WHERE resolution_hours > sla_hours)
             / NULLIF(COUNT(*) FILTER (WHERE resolution_hours IS NOT NULL AND sla_hours IS NOT NULL), 0), 2),
       %(run_id)s, now()
FROM base
GROUP BY category, priority
"""

# Which assignees are in-house is a business assumption: 'vendor tech' is treated as an external contractor.
_ASSIGNEE = f"""
INSERT INTO gold.assignee_performance
WITH base AS (
    SELECT COALESCE(assigned_to, 'UNASSIGNED') AS assigned_to,
           CASE WHEN assigned_to IS NULL THEN 'UNASSIGNED'
                WHEN assigned_to IN ('maintenance team', 'overnight crew', 'Joe (in-house)') THEN 'IN_HOUSE'
                ELSE 'EXTERNAL_VENDOR' END AS assignee_type,
           status, cost, {_RESOLUTION_HOURS} AS resolution_hours
    FROM silver.tickets
)
SELECT assigned_to, assignee_type,
       COUNT(*),
       COUNT(*) FILTER (WHERE status IN ('Resolved', 'Closed')),
       COUNT(*) FILTER (WHERE status = 'Escalated'),
       ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'Escalated') / COUNT(*), 2),
       COUNT(cost),
       SUM(cost),
       ROUND(AVG(cost), 2),
       ROUND(AVG(resolution_hours)::numeric, 2),
       %(run_id)s, now()
FROM base
GROUP BY assigned_to, assignee_type
"""

_BACKLOG = """
INSERT INTO gold.open_ticket_backlog
WITH snap AS (SELECT MAX(created_at)::date AS snapshot_date FROM silver.tickets),
open_tickets AS (
    SELECT COALESCE(building, 'Unknown') AS building, COALESCE(priority, 'UNSET') AS priority,
           (snap.snapshot_date - t.created_at::date) AS age_days, snap.snapshot_date
    FROM silver.tickets t CROSS JOIN snap
    WHERE t.status IN ('Open', 'In Progress', 'Pending Vendor', 'Escalated')
)
SELECT building, priority,
       CASE WHEN age_days <= 7 THEN '0-7d' WHEN age_days <= 30 THEN '8-30d'
            WHEN age_days <= 90 THEN '31-90d' ELSE '90d+' END AS age_bucket,
       COUNT(*), ROUND(AVG(age_days), 1), MAX(snapshot_date), %(run_id)s, now()
FROM open_tickets
GROUP BY building, priority, age_bucket
"""


def build_gold(run_id: str, rec: StageRecord) -> None:
    counts = {}
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM silver.tickets")
        rec.rows_in = cur.fetchone()[0]
        cur.execute("TRUNCATE gold.category_sla_performance, gold.assignee_performance, gold.open_ticket_backlog")
        for table, sql in (("category_sla_performance", _CATEGORY_SLA), ("assignee_performance", _ASSIGNEE),
                           ("open_ticket_backlog", _BACKLOG)):
            cur.execute(sql, {"run_id": run_id})
            counts[table] = cur.rowcount
    rec.rows_out = sum(counts.values())
    rec.details = {"rows_per_table": counts}
    log.info("Gold: built from %d silver tickets -> %s", rec.rows_in, counts)
