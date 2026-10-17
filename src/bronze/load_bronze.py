"""Bronze load: raw CSV values stored as text, untouched, with lineage columns. Idempotent via row_hash."""
import csv
import hashlib
import json
import logging
import time
from pathlib import Path

from psycopg2.extras import execute_values

from src.db import ROOT, transaction
from src.lineage import StageRecord

log = logging.getLogger(__name__)

SOURCE_COLUMNS = [
    "ticket_id", "created_at", "resolved_at", "category", "priority", "status", "building",
    "description", "submitted_by", "assigned_to", "resolution_notes", "cost", "sla_hours",
]
_SEP = "\x1f"  # unit separator: cannot appear in a CSV text field by accident


def row_hash(values: list[str], extra: dict | None) -> str:
    payload = _SEP.join(values)
    if extra:
        payload += _SEP + json.dumps(extra, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_rows(path: Path):
    """Yield (row_number, known_values, extra_columns) without altering any value."""
    with path.open(newline="", encoding="utf-8") as fh:
        for n, rec in enumerate(csv.DictReader(fh), start=1):
            values = [rec.get(col) or "" for col in SOURCE_COLUMNS]
            extra = {k: v for k, v in rec.items() if k not in SOURCE_COLUMNS and k is not None}
            yield n, values, extra or None


def load_bronze(run_id: str, rec: StageRecord, source: Path) -> None:
    t0 = time.monotonic()
    source_file = str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source)
    rows = [
        (*values, json.dumps(extra) if extra else None, source_file, n, run_id, row_hash(values, extra))
        for n, values, extra in read_rows(source)
    ]
    columns = ", ".join(SOURCE_COLUMNS + ["extra_columns", "source_file", "source_row_number", "batch_id", "row_hash"])

    with transaction() as conn, conn.cursor() as cur:
        inserted = execute_values(
            cur,
            f"INSERT INTO bronze.raw_tickets ({columns}) VALUES %s ON CONFLICT (row_hash) DO NOTHING RETURNING 1",
            rows, page_size=1000, fetch=True,
        )
        n_new = len(inserted)
        skipped = len(rows) - n_new
        cur.execute(
            """INSERT INTO bronze.ingestion_log
               (batch_id, source_file, total_rows_in_file, rows_inserted_new, rows_skipped_duplicate, status, duration_ms)
               VALUES (%s, %s, %s, %s, %s, 'SUCCESS', %s)""",
            (run_id, source_file, len(rows), n_new, skipped, int((time.monotonic() - t0) * 1000)),
        )

    rec.rows_in, rec.rows_out = len(rows), n_new
    rec.details = {"source_file": source_file, "skipped_duplicate": skipped}
    log.info("Bronze: read %d rows from %s, inserted %d new, skipped %d already-ingested",
             len(rows), source_file, n_new, skipped)
