"""Silver build: bronze -> typed, cleaned, deduplicated tickets + DLQ. Full rebuild in one transaction."""
import json
import logging
import os
from collections import Counter
from pathlib import Path

from psycopg2.extras import RealDictCursor, execute_values

from src.bronze.load_bronze import SOURCE_COLUMNS  # single source of truth
from src.db import ROOT, transaction
from src.lineage import StageRecord
from src.silver.dlq import invalid_id_reason, missing_timeline_reason
from src.silver.rules import clean_row

log = logging.getLogger(__name__)
# Columns produced by rules.clean_row, in table order; ticket_id, the flags and the lineage columns are added around them.
CLEANED_COLUMNS = [
    "created_at", "resolved_at", "category", "category_raw", "category_swap_corrected", "category_source",
    "priority", "status", "building", "description", "submitted_by", "assigned_to", "resolution_notes",
    "cost", "sla_hours",
]
SILVER_COLUMNS = ["ticket_id", *CLEANED_COLUMNS, "data_quality_flags", "source_row_hash", "bronze_batch_id", "silver_run_id"]

# Written by agents.semantic_agent; used whenever it exists. The hand-built file is the fallback and the agent's test set.
AGENT_MAPPING_FILE = ROOT / "config" / "category_mapping_agent.json"
BASELINE_MAPPING_FILE = ROOT / "config" / "category_mapping.json"


def load_mapping_file(path: Path) -> dict:
    """Return {"mapping", "swapped", "unmapped"} from either the agent file or the flat hand-built file."""
    data = json.loads(path.read_text())
    if "mapping" in data:
        return {"mapping": data["mapping"], "swapped": data.get("swapped", {}), "unmapped": data.get("unmapped", [])}
    return {"mapping": {k: v for k, v in data.items() if not k.startswith("_")}, "swapped": {}, "unmapped": []}


def active_mapping() -> tuple[str, dict]:
    """("agent" | "baseline", mapping). CATEGORY_MAPPING=baseline forces the hand-built file."""
    if os.getenv("CATEGORY_MAPPING", "agent") != "baseline" and AGENT_MAPPING_FILE.exists():
        return "agent", load_mapping_file(AGENT_MAPPING_FILE)
    return "baseline", load_mapping_file(BASELINE_MAPPING_FILE)

# Junk ticket_ids (N/A, blank, NULL) repeat, so they must not collapse into one "duplicate" group.
_BRONZE_QUERY = """
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY CASE WHEN btrim(ticket_id) ~ '^TKT-[0-9]+$' THEN btrim(ticket_id) ELSE 'junk-' || bronze_id END
        ORDER BY ingested_at DESC, bronze_id DESC
    ) AS rn
    FROM bronze.raw_tickets
"""


def build_silver(run_id: str, rec: StageRecord) -> None:
    mapping_source, mapping = active_mapping()
    log.info("Silver: category mapping from %s (%d spellings, %d swapped values)", mapping_source,
             len(mapping["mapping"]), len(mapping["swapped"]))
    tickets, rejected = [], []
    flag_counts, reject_counts = Counter(), Counter()
    superseded = 0

    with transaction() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_BRONZE_QUERY)
            bronze_rows = cur.fetchall()

        for raw in bronze_rows:
            if raw["rn"] > 1:  # an older version of a ticket we already have; bronze keeps the history
                superseded += 1
                continue
            reason = invalid_id_reason(raw["ticket_id"])
            cleaned, flags = (None, [])
            if reason is None:
                cleaned, flags = clean_row(raw, mapping["mapping"], mapping["swapped"], mapping_source)
                reason = missing_timeline_reason(cleaned)
            if reason:
                reject_counts[reason] += 1
                payload = {c: raw[c] for c in SOURCE_COLUMNS}
                rejected.append((raw["ticket_id"], reason, json.dumps(payload),
                                 raw["row_hash"], raw["batch_id"], run_id))
                continue
            flag_counts.update(flags)
            tickets.append((raw["ticket_id"].strip(), *(cleaned[c] for c in CLEANED_COLUMNS), sorted(flags),
                            raw["row_hash"], raw["batch_id"], run_id))

        with conn.cursor() as cur:
            cur.execute("TRUNCATE silver.tickets, silver.rejected_rows RESTART IDENTITY")
            execute_values(cur, f"INSERT INTO silver.tickets ({', '.join(SILVER_COLUMNS)}) VALUES %s",
                           tickets, page_size=1000)
            execute_values(
                cur,
                """INSERT INTO silver.rejected_rows
                   (ticket_id_raw, reason, raw_payload, source_row_hash, bronze_batch_id, silver_run_id) VALUES %s""",
                rejected, page_size=1000,
            )

    rec.rows_in, rec.rows_out, rec.rows_rejected = len(bronze_rows), len(tickets), len(rejected)
    # Rows this run added to bronze, so a bad incoming file is judged on its own and not diluted by history.
    batch_in = sum(1 for raw in bronze_rows if raw["batch_id"] == run_id)
    batch_rejected = sum(1 for r in rejected if r[4] == run_id)
    rec.details = {"category_mapping": mapping_source, "reject_reasons": dict(reject_counts), "flag_counts": dict(flag_counts),
                   "superseded_versions": superseded, "batch_rows_in": batch_in, "batch_rows_rejected": batch_rejected}
    log.info("Silver: %d bronze rows -> %d tickets, %d to DLQ %s, %d superseded versions dropped",
             len(bronze_rows), len(tickets), len(rejected), dict(reject_counts), superseded)
    log.info("Silver flags: %s", dict(flag_counts.most_common()))
