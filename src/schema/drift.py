"""Schema drift detection: infer a schema with pandera, diff it against the committed snapshot.

Deterministic on purpose: column/dtype comparison is a solved problem, so no LLM is involved. Nothing here
changes the database; it reports drift and prints the DDL a human would review and add to sql/.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import pandera.pandas as pa

from src.db import ROOT

log = logging.getLogger(__name__)

SNAPSHOT = ROOT / "schema_snapshots" / "raw_tickets_schema.json"
SAMPLE_ROWS = 10_000  # inference does not need the whole file

_SQL_TYPES = {"string": "TEXT", "int64": "BIGINT", "float64": "NUMERIC", "bool": "BOOLEAN", "datetime64[ns]": "TIMESTAMP"}


def _dtype_name(dtype) -> str:
    name = str(dtype)
    return "string" if name.startswith("string") else name


def infer_snapshot(csv_path: Path, baseline: dict | None = None) -> dict:
    df = pd.read_csv(csv_path, nrows=SAMPLE_ROWS)
    if df.empty:
        # Header-only file: there are no values to infer types from, so only column names are compared.
        # Known columns keep their snapshot type; new ones are reported as nullable text.
        return {c: (baseline or {}).get(c, {"dtype": "string", "nullable": True}) for c in df.columns}
    schema = pa.infer_schema(df)
    return {name: {"dtype": _dtype_name(col.dtype), "nullable": bool(col.nullable)}
            for name, col in schema.columns.items()}


def diff(baseline: dict, current: dict) -> dict:
    return {
        "added": {c: current[c] for c in current.keys() - baseline.keys()},
        "removed": {c: baseline[c] for c in baseline.keys() - current.keys()},
        "dtype_changed": {c: (baseline[c]["dtype"], current[c]["dtype"])
                          for c in baseline.keys() & current.keys() if baseline[c]["dtype"] != current[c]["dtype"]},
        "nullability_changed": {c: (baseline[c]["nullable"], current[c]["nullable"])
                                for c in baseline.keys() & current.keys()
                                # only loosening matters; a small clean file looking "stricter" is sampling noise
                                if not baseline[c]["nullable"] and current[c]["nullable"]},
    }


def has_drift(d: dict) -> bool:
    return any(d.values())


def _render(source: Path, d: dict) -> str:
    lines = ["# Schema drift report", "", f"Compared `{source.name}` against `{SNAPSHOT.relative_to(ROOT)}`.", ""]
    if not has_drift(d):
        return "\n".join(lines + ["**No drift detected.**", ""])
    lines += ["**Drift detected. Nothing was applied automatically.** Bronze already keeps unknown columns in "
              "`bronze.raw_tickets.extra_columns`, so no data is lost while this is reviewed.", ""]
    if d["added"]:
        lines += ["## Added columns", ""] + [f"- `{c}` ({m['dtype']}, nullable={m['nullable']})" for c, m in sorted(d["added"].items())]
    if d["removed"]:
        lines += ["", "## Removed columns", "", "Bronze stores these as empty; silver keeps the column (NULL for new rows)."]
        lines += [f"- `{c}`" for c in sorted(d["removed"])]
    if d["dtype_changed"]:
        lines += ["", "## Type changes", ""] + [f"- `{c}`: {a} -> {b}" for c, (a, b) in sorted(d["dtype_changed"].items())]
    if d["nullability_changed"]:
        lines += ["", "## Nullability changes", ""] + [f"- `{c}`: nullable {a} -> {b}" for c, (a, b) in sorted(d["nullability_changed"].items())]
    lines += ["", "## Proposed migration (review, then add to `sql/`)", "", "```sql"]
    lines += [f"ALTER TABLE silver.tickets ADD COLUMN IF NOT EXISTS {c} {_SQL_TYPES.get(m['dtype'], 'TEXT')};"
              for c, m in sorted(d["added"].items())] or ["-- no column additions"]
    lines += ["```", "", "Type changes need a human decision (cast rule, backfill, or new column); they are never auto-proposed.", ""]
    return "\n".join(lines)


def report_path(source: Path) -> Path:
    return ROOT / "reports" / f"schema_drift_{source.stem}.md"


def check_drift(csv_path: Path, write_report: bool = True) -> dict:
    """Diff csv_path against the snapshot. Creates the snapshot on first use."""
    if not SNAPSHOT.exists():
        current = infer_snapshot(csv_path)
        save_snapshot(current)
        log.info("Schema snapshot created from %s", csv_path.name)
        return diff(current, current)
    baseline = json.loads(SNAPSHOT.read_text())
    d = diff(baseline, infer_snapshot(csv_path, baseline))
    if write_report:
        report_path(csv_path).parent.mkdir(exist_ok=True)
        report_path(csv_path).write_text(_render(csv_path, d))
    return d


def save_snapshot(snapshot: dict) -> None:
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect schema drift in a source CSV")
    parser.add_argument("file", type=Path)
    parser.add_argument("--accept", action="store_true", help="overwrite the snapshot with this file's schema")
    args = parser.parse_args()
    if args.accept:
        if pd.read_csv(args.file, nrows=1).empty:
            print(f"{args.file} has no data rows; refusing to build a snapshot from column names alone")
            return 1
        save_snapshot(infer_snapshot(args.file))
        print(f"Snapshot updated from {args.file}")
        return 0
    d = check_drift(args.file)
    print(report_path(args.file).read_text())
    return 1 if has_drift(d) else 0


if __name__ == "__main__":
    sys.exit(main())
