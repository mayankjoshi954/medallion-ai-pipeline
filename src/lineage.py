"""Persist what every stage and every LLM call did, in tables you can SELECT from."""
import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.db import transaction

log = logging.getLogger(__name__)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


@dataclass
class StageRecord:
    rows_in: int | None = None
    rows_out: int | None = None
    rows_rejected: int | None = None
    details: dict = field(default_factory=dict)


@contextmanager
def stage_run(run_id: str, stage: str):
    """Record a stage's outcome on its own connection so a failed stage is still logged after its rollback."""
    rec = StageRecord()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    log.info("[%s] stage %s started", run_id, stage)
    try:
        yield rec
    except BaseException as exc:  # includes SystemExit from an agent, so every failure leaves a FAILED row
        _write(run_id, stage, "FAILED", started, t0, rec, repr(exc))
        log.error("[%s] stage %s FAILED: %s", run_id, stage, exc)
        raise
    _write(run_id, stage, "SUCCESS", started, t0, rec, None)
    log.info(
        "[%s] stage %s done in %d ms: in=%s out=%s rejected=%s",
        run_id, stage, int((time.monotonic() - t0) * 1000), rec.rows_in, rec.rows_out, rec.rows_rejected,
    )


# Which row count represents "volume" per stage; bronze rows_out is 0 on an idempotent re-run, so it can't be used.
VOLUME_METRIC = {"bronze": "rows_in", "silver": "rows_out", "gold": "rows_out"}


def volume_vs_previous(cur, run_id: str) -> dict[str, tuple[int | None, int | None]]:
    """{stage: (this run's volume, volume of the previous successful run on the same source file)}.

    One definition of "previous run", shared by the volume alert and the run summary. Only successful stages that
    have a volume appear; quality, skipped and failed stages do not.
    """
    cur.execute("""SELECT stage, started_at, rows_in, rows_out, details->>'source_file' FROM lineage.pipeline_runs
                   WHERE run_id = %s AND status = 'SUCCESS'""", (run_id,))
    volumes = {}
    for stage, started_at, rows_in, rows_out, source_file in cur.fetchall():
        col = VOLUME_METRIC.get(stage)
        if col is None:
            continue
        # col comes from the VOLUME_METRIC constant above, never from input
        cur.execute(f"""SELECT {col} FROM lineage.pipeline_runs
                        WHERE stage = %s AND status = 'SUCCESS' AND run_id <> %s AND started_at < %s
                          AND (details->>'source_file') IS NOT DISTINCT FROM %s
                        ORDER BY started_at DESC LIMIT 1""", (stage, run_id, started_at, source_file))
        previous = cur.fetchone()
        volumes[stage] = (rows_in if col == "rows_in" else rows_out, previous[0] if previous else None)
    return volumes


def record_skipped(run_id: str, stage: str, reason: str) -> None:
    """Leave a SKIPPED row so a deliberately unbuilt stage is visible in lineage, not just absent."""
    _write(run_id, stage, "SKIPPED", datetime.now(timezone.utc), time.monotonic(), StageRecord(), reason)


def _write(run_id, stage, status, started, t0, rec: StageRecord, error):
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO lineage.pipeline_runs
               (run_id, stage, status, started_at, finished_at, duration_ms,
                rows_in, rows_out, rows_rejected, details, error_message)
               VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s)""",
            (run_id, stage, status, started, int((time.monotonic() - t0) * 1000),
             rec.rows_in, rec.rows_out, rec.rows_rejected, json.dumps(rec.details), error),
        )


def log_agent_call(agent_name, model, prompt_tokens, completion_tokens, cost_usd, duration_ms,
                   cache_hit, status, output_file=None) -> None:
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO lineage.agent_runs
               (agent_name, model, prompt_tokens, completion_tokens, estimated_cost_usd,
                duration_ms, cache_hit, status, output_file)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (agent_name, model, prompt_tokens, completion_tokens, cost_usd, duration_ms,
             cache_hit, status, output_file),
        )
