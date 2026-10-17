"""Stage orchestration. Each stage is idempotent, so any subset can be re-run safely."""
import logging
from pathlib import Path

from src import observability
from src.bronze.load_bronze import load_bronze
from src.db import ROOT, apply_ddl
from src.gold.build_gold import build_gold
from src.lineage import new_run_id, record_skipped, stage_run
from src.quality.run_gx import AGENT_EXPECTATIONS_FILE, run_agent_checks, run_baseline, run_gate
from src.schema.drift import check_drift, has_drift, report_path
from src.silver.clean_silver import build_silver

log = logging.getLogger(__name__)

STAGES = ("bronze", "silver", "gold")
DEFAULT_SOURCE = ROOT / "data" / "raw_tickets.csv"


def _run_agents(run_id: str) -> None:
    """Optional LLM step: refresh the agents' auto-applied outputs. A failing agent never stops the pipeline."""
    from agents import quality_agent, semantic_agent  # imported here so the core pipeline needs no LLM packages

    for name, agent in (("semantic_agent", semantic_agent), ("quality_agent", quality_agent)):
        try:
            with stage_run(run_id, name) as rec:
                agent.run(rec)
        except (Exception, SystemExit) as exc:  # SystemExit: missing key or quota, raised by llm_client
            observability.record_alert(run_id, name, "WARNING", "agent_failed", None,
                                       f"{name} failed; its last good output stays in use: {exc}")


def run(stages: tuple[str, ...], source: Path = DEFAULT_SOURCE, with_agents: bool = False) -> int:
    """Run the requested stages in order. Returns a process exit code (0 ok, 2 blocked by a CRITICAL alert)."""
    run_id = new_run_id()
    log.info("Pipeline run %s starting: stages=%s", run_id, ",".join(stages))
    apply_ddl()
    blocked = False

    if "bronze" in stages:
        try:
            drift = check_drift(source)
        except Exception as exc:  # drift is advisory: it must never stop bronze from landing the file
            log.exception("Schema drift check failed")
            observability.record_alert(run_id, "bronze", "WARNING", "schema_drift_check_failed", None,
                                       f"Schema drift check failed on {source.name}: {exc!r}")
            drift = {}
        if has_drift(drift):
            observability.record_alert(
                run_id, "bronze", "WARNING", "schema_drift", sum(len(v) for v in drift.values()),
                f"Source schema differs from snapshot; see {report_path(source).relative_to(ROOT)}")
        with stage_run(run_id, "bronze") as rec:
            load_bronze(run_id, rec, source)
        if rec.rows_in == 0:
            observability.record_alert(run_id, "bronze", "WARNING", "empty_source", 0,
                                       f"{source.name} has no data rows; silver and gold reflect earlier loads only")
        with stage_run(run_id, "quality_baseline") as rec:
            run_baseline(run_id, rec)

    if with_agents:
        _run_agents(run_id)
    if "bronze" in stages and AGENT_EXPECTATIONS_FILE.exists():
        with stage_run(run_id, "quality_agent_checks") as rec:
            run_agent_checks(run_id, rec)

    if "silver" in stages:
        with stage_run(run_id, "silver") as rec:
            build_silver(run_id, rec)
        with stage_run(run_id, "quality_gate") as rec:
            gate_failed = run_gate(run_id, rec)
        if gate_failed:
            observability.record_alert(run_id, "silver", "CRITICAL", "gx_critical_failure", None,
                                       "Silver failed a critical Great Expectations check; see lineage.quality_results")
        blocked = observability.check_reject_ratio(run_id) or gate_failed

    if "gold" in stages:
        if "silver" not in stages:  # gold-only run: honour the verdict of the silver build it would publish
            blocked = observability.latest_silver_blocked()
        if blocked:
            log.error("Skipping gold: silver failed a CRITICAL check")
            record_skipped(run_id, "gold", "blocked by a CRITICAL silver alert; see lineage.alerts")
        else:
            with stage_run(run_id, "gold") as rec:
                build_gold(run_id, rec)

    observability.check_volume_drop(run_id)
    observability.dispatch_notifications()
    log.info("Pipeline run %s finished (blocked=%s); see lineage.pipeline_runs", run_id, blocked)
    return 2 if blocked else 0
