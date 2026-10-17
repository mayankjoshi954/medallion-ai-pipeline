"""Great Expectations runner: bronze baseline (how dirty is the raw data) and silver gate (is it safe for gold).

Runs against pandas DataFrames pulled from Postgres. At larger scale, point GX at a SQL datasource instead so
validation is pushed down to the database rather than loading rows into memory.
"""
import json
import logging

import great_expectations as gx
import great_expectations.expectations as gxe
import pandas as pd
from psycopg2.extras import execute_values

from src.db import ROOT, transaction
from src.lineage import StageRecord

log = logging.getLogger(__name__)

_BRONZE_SQL = """SELECT ticket_id, created_at, resolved_at, category, priority, status, building,
                        description, submitted_by, assigned_to, resolution_notes, cost, sla_hours
                 FROM bronze.raw_tickets"""
_SILVER_SQL = "SELECT * FROM silver.tickets"
# Written by agents.quality_agent; run as the bronze_agent suite whenever it exists.
AGENT_EXPECTATIONS_FILE = ROOT / "config" / "expectations_agent.json"


def _load(sql: str) -> pd.DataFrame:
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def _silver_frame() -> pd.DataFrame:
    df = _load(_SILVER_SQL)
    for col in ("cost", "sla_hours"):
        df[col] = pd.to_numeric(df[col])
    for col in ("created_at", "resolved_at"):
        df[col] = pd.to_datetime(df[col])
    return df


def _validate(suite_name: str, df: pd.DataFrame, specs: list[dict]) -> tuple[list[dict], int, int]:
    """Return (one result dict per expectation, rows failing any check, rows failing a blocking check)."""
    expectations = [
        getattr(gxe, s["type"])(**s["kwargs"], meta={"severity": s["severity"], "why": s["why"],
                                                    "kind": s.get("kind", "repairable")})
        for s in specs
    ]
    ctx = gx.get_context(mode="ephemeral")
    asset = ctx.data_sources.add_pandas(f"{suite_name}_src").add_dataframe_asset(suite_name)
    batch = asset.add_batch_definition_whole_dataframe("whole").get_batch(batch_parameters={"dataframe": df})
    outcome = batch.validate(gx.ExpectationSuite(name=suite_name, expectations=expectations),
                             result_format={"result_format": "COMPLETE"})

    results, bad_rows, blocking_rows = [], set(), set()
    for res in outcome.results:
        cfg = res.expectation_config
        r = res.result
        failed_idx = r.get("unexpected_index_list") or []
        bad_rows.update(failed_idx)
        if cfg.meta["kind"] == "blocking":
            blocking_rows.update(failed_idx)
        kwargs = {k: v for k, v in cfg.kwargs.items() if k not in ("column", "batch_id", "mostly")}
        results.append({
            "expectation": cfg.type,
            "column": cfg.kwargs.get("column") or cfg.kwargs.get("column_A"),
            "severity": cfg.meta["severity"],
            "success": bool(res.success),
            "element_count": r.get("element_count"),
            "unexpected_count": r.get("unexpected_count"),
            "unexpected_percent": round(r["unexpected_percent"], 2) if r.get("unexpected_percent") is not None else None,
            "observed_value": r.get("observed_value"),
            "detail": json.dumps(kwargs, default=str)[:300],
            "why": cfg.meta["why"],
        })
    return results, len(bad_rows), len(blocking_rows)


def _persist(run_id: str, suite: str, results: list[dict]) -> None:
    rows = [
        (run_id, suite, r["expectation"], r["column"], r["severity"], r["success"], r["element_count"],
         r["unexpected_count"], r["unexpected_percent"],
         None if r["observed_value"] is None else str(r["observed_value"]), r["detail"], r["why"])
        for r in results
    ]
    with transaction() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO lineage.quality_results
               (run_id, suite, expectation, column_name, severity, success, element_count,
                unexpected_count, unexpected_percent, observed_value, detail, why) VALUES %s""",
            rows,
        )


def _run(run_id: str, rec: StageRecord, suite: str, df: pd.DataFrame, specs: list[dict] | None = None) -> list[dict]:
    if specs is None:
        specs = json.loads((ROOT / "config" / "expectations.json").read_text())[suite]
    results, bad_rows, blocking_rows = _validate(suite, df, specs)
    _persist(run_id, suite, results)
    failed = [r for r in results if not r["success"]]
    rec.rows_in = len(df)
    rec.details = {
        "rows_with_any_issue": bad_rows,
        "pct_rows_with_any_issue": round(100.0 * bad_rows / max(len(df), 1), 2),
        "rows_unusable": blocking_rows,
        "pct_rows_unusable": round(100.0 * blocking_rows / max(len(df), 1), 2),
        "expectations": len(results),
        "failed": len(failed),
        "failed_critical": sum(1 for r in failed if r["severity"] == "critical"),
    }
    log.info("GX %s: %d/%d expectations passed; %d of %d rows (%.1f%%) fail any check, %d (%.2f%%) fail a blocking check",
             suite, len(results) - len(failed), len(results), bad_rows, len(df),
             rec.details["pct_rows_with_any_issue"], blocking_rows, rec.details["pct_rows_unusable"])
    return results


def run_baseline(run_id: str, rec: StageRecord) -> None:
    """Measure raw bronze quality. Failures are expected and are not alerts: this is the 'before' picture."""
    _run(run_id, rec, "bronze_baseline", _load(_BRONZE_SQL))


def run_agent_checks(run_id: str, rec: StageRecord) -> None:
    """Run the Data Quality Agent's expectations against bronze. Measurement only: never blocks a stage."""
    specs = json.loads(AGENT_EXPECTATIONS_FILE.read_text())["bronze_agent"]
    if not specs:
        rec.details = {"expectations": 0}
        return
    _run(run_id, rec, "bronze_agent", _load(_BRONZE_SQL), specs)


def run_gate(run_id: str, rec: StageRecord) -> bool:
    """Validate silver. Returns True if a critical expectation failed (gold must not be built)."""
    results = _run(run_id, rec, "silver_gate", _silver_frame())
    return any(not r["success"] and r["severity"] == "critical" for r in results)
