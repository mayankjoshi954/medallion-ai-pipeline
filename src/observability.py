"""Threshold alerts. Detection and persistence are real; Slack delivery is a stub behind ALERTS_ENABLED."""
import json
import logging
import os
import urllib.request

from src.db import transaction
from src.lineage import volume_vs_previous

log = logging.getLogger(__name__)

REJECT_RATIO_WARN = 0.05
REJECT_RATIO_CRITICAL = 0.30  # beyond this the source is too dirty to publish gold from
VOLUME_DROP_RATIO = 0.80      # alert if a stage emits <80% of what it did on the previous successful run


def record_alert(run_id, stage, severity, metric, value, message) -> None:
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO lineage.alerts (run_id, stage, severity, metric_name, metric_value, message)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, stage, severity, metric, value, message),
        )
    getattr(log, "error" if severity == "CRITICAL" else "warning")("ALERT %s [%s] %s", severity, stage, message)


def check_reject_ratio(run_id: str) -> bool:
    """Alert on a high DLQ share, measured on the rows this run ingested and on all of bronze.

    The batch ratio catches a broken incoming file that would be diluted by a large history; the overall ratio
    covers silver-only runs. Returns True if either is CRITICAL (gold must not run).
    """
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT rows_in, rows_rejected, (details->>'batch_rows_in')::int, (details->>'batch_rows_rejected')::int
               FROM lineage.pipeline_runs WHERE run_id = %s AND stage = 'silver' AND status = 'SUCCESS'""",
            (run_id,),
        )
        row = cur.fetchone()
    if not row:
        return False
    # (metric, wording, rows, rejected): the batch scope first, so a warning names the more specific one
    scopes = [("reject_ratio_batch", "of this run's new rows", row[2], row[3]),
              ("reject_ratio", "of all bronze rows", row[0], row[1])]
    ratios = [(metric, scope, (rejected or 0) / total) for metric, scope, total, rejected in scopes if total]
    for metric, scope, ratio in ratios:
        if ratio > REJECT_RATIO_CRITICAL:
            record_alert(run_id, "silver", "CRITICAL", metric, round(ratio, 4),
                         f"{ratio:.1%} {scope} went to the DLQ (limit {REJECT_RATIO_CRITICAL:.0%})")
            return True
    for metric, scope, ratio in ratios:
        if ratio > REJECT_RATIO_WARN:
            record_alert(run_id, "silver", "WARNING", metric, round(ratio, 4),
                         f"{ratio:.1%} {scope} went to the DLQ (warn above {REJECT_RATIO_WARN:.0%})")
            break
    return False


def latest_silver_blocked() -> bool:
    """True if the most recent silver build raised a CRITICAL alert, so a gold-only run can't publish it."""
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM lineage.alerts
                   WHERE stage = 'silver' AND severity = 'CRITICAL' AND run_id = (
                       SELECT run_id FROM lineage.pipeline_runs
                       WHERE stage = 'silver' AND status = 'SUCCESS' ORDER BY started_at DESC LIMIT 1))"""
        )
        return cur.fetchone()[0]


def check_volume_drop(run_id: str) -> None:
    """Compare each stage's volume with its previous successful run of the same source file."""
    with transaction() as conn, conn.cursor() as cur:
        volumes = volume_vs_previous(cur, run_id)
    for stage, (current, previous) in volumes.items():
        if previous and current is not None and current < previous * VOLUME_DROP_RATIO:
            record_alert(run_id, stage, "WARNING", "volume_drop", current,
                         f"{stage} volume {current} is below {VOLUME_DROP_RATIO:.0%} of previous run ({previous})")


def dispatch_notifications() -> None:
    """Post unsent alerts to Slack. Disabled by default; alerts are always queryable in lineage.alerts."""
    if os.getenv("ALERTS_ENABLED", "false").lower() != "true":
        log.info("Alert delivery disabled (ALERTS_ENABLED!=true); alerts stay in lineage.alerts")
        return
    slack_url = os.getenv("SLACK_WEBHOOK_URL")
    if not slack_url:
        log.warning("ALERTS_ENABLED=true but SLACK_WEBHOOK_URL is not set; no notifications sent")
        return
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("SELECT alert_id, severity, stage, message FROM lineage.alerts WHERE notified_at IS NULL")
        for alert_id, severity, stage, message in cur.fetchall():
            if _post_slack(slack_url, f"[{severity}] ticket-pipeline/{stage}: {message}"):
                cur.execute(
                    "UPDATE lineage.alerts SET notified_at = now(), notified_channel = 'slack' WHERE alert_id = %s",
                    (alert_id,),
                )


def _post_slack(url: str, text: str) -> bool:
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310 - URL comes from operator-set env var
            return True
    except OSError as exc:
        log.warning("Slack webhook delivery failed: %s", exc)
        return False
