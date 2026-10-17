"""Data Quality Agent. Profiles bronze, proposes cleaning/validation rules with the reason each matters.

Runs without a human in the loop. Expectations that pass the guardrails are written to config/expectations_agent.json
and run automatically against bronze on every pipeline run (suite bronze_agent, measurement only, never blocking).
The generated SQL/Python for each finding is never executed: running LLM-written code unattended is the one step
this project does not automate. Findings are reported in proposals/quality_agent_output.md.
"""
import argparse
import ast
import json
import logging
import re
from datetime import datetime, timezone

import great_expectations.expectations as gxe

from agents.llm_client import complete_json
from src.bronze.load_bronze import SOURCE_COLUMNS
from src.db import ROOT, transaction
from src.lineage import StageRecord
from src.log import configure_logging
from src.quality.run_gx import AGENT_EXPECTATIONS_FILE

log = logging.getLogger(__name__)

AGENT = "quality_agent"
ALLOWED_EXPECTATIONS = {
    "ExpectColumnValuesToNotBeNull", "ExpectColumnValuesToBeUnique", "ExpectColumnValuesToMatchRegex",
    "ExpectColumnValuesToBeInSet", "ExpectColumnValuesToNotBeInSet", "ExpectColumnValuesToBeBetween",
    "ExpectTableRowCountToBeBetween",
}
ACTIONS = {"normalize", "null_out", "flag", "reject_to_dlq"}
SEVERITIES = {"critical", "warning", "info"}
FORBIDDEN_SQL = re.compile(r"\b(drop|delete|truncate|alter|insert|update|grant|create|merge)\b", re.I)

SYSTEM = f"""You are a senior data engineer reviewing a statistical profile of a RAW (bronze) table of facility-support tickets.
You see aggregates and sample values only. Use ONLY evidence present in the profile; never invent columns, counts or values.
If the evidence for a rule is weak, leave it out. Category spelling normalization is handled by a separate agent: do not map categories.
For numeric columns, numeric_distribution gives percentiles and IQR outliers; say whether frequently repeated outlier values
look like real measurements or placeholder sentinels.

For every issue: quote the numbers that prove it, explain WHY it matters for downstream reporting (SLA, cost, backlog), propose one
concrete rule, and give an implementation.

Respond with ONE JSON object and nothing else, exactly this shape:
{{
  "findings": [{{
    "column": "<one of {SOURCE_COLUMNS} or 'cross-column'>",
    "issue": "<short>",
    "evidence": "<quote numbers from the profile>",
    "why_it_matters": "<business impact, one or two sentences>",
    "proposed_rule": "<plain English>",
    "action": "<{ '|'.join(sorted(ACTIONS)) }>",
    "severity": "<{ '|'.join(sorted(SEVERITIES)) }>",
    "implementation": {{"language": "python|sql", "code": "<a pure function or a SELECT/CASE expression; no DDL or DML>"}}
  }}],
  "expectations": [{{
    "type": "<one of {sorted(ALLOWED_EXPECTATIONS)}>",
    "kwargs": {{}},
    "severity": "<critical|warning|info>",
    "why": "<one sentence>"
  }}]
}}
At most 15 findings, ordered by severity. Expectations must reference only the columns listed above (all are TEXT in bronze)."""


_NUMERIC = r"^-?[0-9]+(\.[0-9]+)?$"
_SHAPE = "regexp_replace(regexp_replace({c}, '[A-Za-z]', 'a', 'g'), '[0-9]', '9', 'g')"


def _counts(cur, sql: str, params=()) -> dict:
    """Run a two-column (value, count) query and return {str(value): count}."""
    cur.execute(sql, params)
    return {str(value): n for value, n in cur.fetchall()}


def _numeric_distribution(cur, col: str) -> dict:
    """Percentiles and IQR outliers of the numeric-looking values, plus the values that make up the outliers."""
    # CASE (not WHERE ... AND cast) guarantees the regex is checked before the cast
    values = f"SELECT CASE WHEN {col} ~ %s THEN {col}::numeric END x FROM bronze.raw_tickets"
    cur.execute(f"""WITH v AS (SELECT x FROM ({values}) t WHERE x IS NOT NULL),
                         q AS (SELECT MIN(x) mn, MAX(x) mx,
                                      PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY x) p01,
                                      PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY x) p25,
                                      PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY x) p50,
                                      PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY x) p75,
                                      PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY x) p99 FROM v)
                    SELECT q.*, (SELECT COUNT(*) FROM v WHERE x < p25 - 1.5 * (p75 - p25) OR x > p75 + 1.5 * (p75 - p25))
                    FROM q""", (_NUMERIC,))
    mn, mx, p01, p25, p50, p75, p99, n_out = cur.fetchone()
    if mn is None:
        return {}
    low, high = p25 - 1.5 * (p75 - p25), p75 + 1.5 * (p75 - p25)
    outliers = _counts(cur, f"SELECT x, COUNT(*) FROM ({values}) t WHERE x < %s OR x > %s GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 8",
                       (_NUMERIC, low, high))
    return {"min": float(mn), "p01": p01, "p25": p25, "median": p50, "p75": p75, "p99": p99, "max": float(mx),
            "iqr_outliers": n_out, "most_common_outlier_values": outliers}


def _profile_column(cur, col: str, total: int) -> dict:
    cur.execute(f"""SELECT COUNT(*) FILTER (WHERE {col} IS NULL OR btrim({col}) = ''), COUNT(DISTINCT {col})
                    FROM bronze.raw_tickets""")
    blank, distinct = cur.fetchone()
    entry = {"blank_pct": round(100.0 * blank / total, 2), "distinct_values": distinct}
    by_count = "GROUP BY 1 ORDER BY 2 DESC, 1"  # the tie-break keeps the prompt, and so its cache key, stable
    if distinct <= 40:
        entry["value_counts"] = _counts(cur, f"SELECT {col}, COUNT(*) FROM bronze.raw_tickets {by_count}")
    else:
        entry["top_shapes"] = _counts(cur, f"SELECT {_SHAPE.format(c=col)}, COUNT(*) FROM bronze.raw_tickets {by_count} LIMIT 12")
        entry["most_common_values"] = _counts(cur, f"SELECT {col}, COUNT(*) FROM bronze.raw_tickets {by_count} LIMIT 12")
    if col in ("cost", "sla_hours"):
        entry["numeric_distribution"] = _numeric_distribution(cur, col)
        cur.execute(f"""SELECT COUNT(*) FILTER (WHERE {col} ~ %s), COUNT(*) FILTER (WHERE {col} ~ '^-[0-9]'),
                               COUNT(*) FILTER (WHERE {col} ~ '^0+(\\.0+)?$') FROM bronze.raw_tickets""", (_NUMERIC,))
        entry["numeric_like"], entry["negative_values"], entry["zero_values"] = cur.fetchone()
        entry["non_numeric_nonblank"] = _counts(
            cur, f"SELECT {col}, COUNT(*) FROM bronze.raw_tickets WHERE {col} !~ %s AND btrim({col}) <> '' {by_count} LIMIT 10",
            (_NUMERIC,))
    return entry


def build_profile() -> dict:
    """SQL-derived profile per column: blank rate, cardinality, values or character shapes, numeric outliers."""
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM bronze.raw_tickets")
        total = cur.fetchone()[0]
        return {"row_count": total, "columns": {col: _profile_column(cur, col, total) for col in SOURCE_COLUMNS}}


def _finding_problem(f: dict) -> str | None:
    """Why a finding fails the guardrails, or None. The code it carries is only checked, never run."""
    impl = f.get("implementation") or {}
    code, lang = impl.get("code", ""), impl.get("language")
    if f.get("action") not in ACTIONS or f.get("severity") not in SEVERITIES:
        return "invalid action/severity"
    if f.get("column") not in SOURCE_COLUMNS + ["cross-column"]:
        return f"unknown column {f.get('column')!r}"
    if lang == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return f"python does not parse: {exc.msg}"
    elif lang == "sql":
        if FORBIDDEN_SQL.search(code):
            return "sql contains DDL/DML keywords"
    else:
        return "implementation language must be python or sql"
    return None


def _expectation_problem(e: dict) -> str | None:
    """Why an expectation fails the guardrails, or None. It will run unattended, so GX itself must accept it."""
    kwargs = e.get("kwargs")
    column = kwargs.get("column") if isinstance(kwargs, dict) else None
    if e.get("type") not in ALLOWED_EXPECTATIONS or not isinstance(kwargs, dict) \
            or (column is not None and column not in SOURCE_COLUMNS) or e.get("severity") not in SEVERITIES:
        return "not in allow-list or bad shape"
    try:
        getattr(gxe, e["type"])(**kwargs)
    except Exception as exc:
        return f"GX rejected the arguments: {str(exc)[:120]}"
    return None


def validate(raw: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Split the model's answer into (accepted findings, accepted expectations, rejection messages)."""
    findings, expectations, rejected = [], [], []
    for f in raw.get("findings", []):
        problem = _finding_problem(f)
        (rejected.append(f"{f.get('column')}: {f.get('issue')} ({problem})") if problem else findings.append(f))
    for e in raw.get("expectations", []):
        problem = _expectation_problem(e)
        column = (e.get("kwargs") or {}).get("column") if isinstance(e.get("kwargs"), dict) else None
        (rejected.append(f"expectation {e.get('type')} on {column} ({problem})") if problem else expectations.append(e))
    return findings, expectations, rejected


def render(findings, expectations, rejected, profile) -> str:
    lines = ["# Data Quality Agent run", "",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} from a profile of {profile['row_count']} bronze rows.", "",
             f"{len(findings)} findings, {len(expectations)} expectations accepted by guardrails, {len(rejected)} rejected.", "",
             f"- **Applied automatically:** the {len(expectations)} accepted expectations, written to "
             f"`{AGENT_EXPECTATIONS_FILE.relative_to(ROOT)}` and run against bronze on every pipeline run (suite `bronze_agent`).",
             "- **Not executed:** the SQL/Python below. It is shown as the agent wrote it; the live cleaning rules are in "
             "`src/silver/rules.py`.", ""]
    for i, f in enumerate(findings, 1):
        impl = f["implementation"]
        lines += [f"## {i}. `{f['column']}`: {f['issue']} ({f['severity']}, action: {f['action']})", "",
                  f"- **Evidence:** {f['evidence']}", f"- **Why it matters:** {f['why_it_matters']}",
                  f"- **Rule:** {f['proposed_rule']}", "", f"```{impl['language']}", impl["code"].strip(), "```", ""]
    if rejected:
        lines += ["## Rejected by guardrails", ""] + [f"- {r}" for r in rejected] + [""]
    if expectations:
        lines += ["## Applied expectations", ""] + [
            f"- `{e['type']}` {json.dumps(e['kwargs'])}: {e.get('why', '')}" for e in expectations] + [""]
    return "\n".join(lines)


def run(rec: StageRecord | None = None, *, dry_run: bool = False, use_cache: bool = True) -> None:
    rec = rec or StageRecord()
    profile = build_profile()
    user = "Bronze profile (JSON):\n" + json.dumps(profile, indent=1)
    raw = complete_json(AGENT, SYSTEM, user, dry_run=dry_run, use_cache=use_cache)
    if raw is None:
        (ROOT / "proposals" / "quality_agent_prompt_preview.txt").write_text(f"{SYSTEM}\n\n{user}\n")
        log.info("Prompt written to proposals/quality_agent_prompt_preview.txt")
        return
    findings, expectations, rejected = validate(raw)
    # Bronze checks only measure; force a non-blocking severity whatever the model suggested.
    applied = [{**e, "severity": "info", "why": e.get("why") or "proposed by the Data Quality Agent"} for e in expectations]
    AGENT_EXPECTATIONS_FILE.write_text(json.dumps({
        "_comment": "Generated by agents.quality_agent and run automatically as the bronze_agent suite. Do not hand-edit.",
        "_generated": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        "bronze_agent": applied,
    }, indent=2) + "\n")
    (ROOT / "proposals" / "quality_agent_output.md").write_text(render(findings, applied, rejected, profile))
    rec.rows_in, rec.rows_out, rec.rows_rejected = profile["row_count"], len(applied), len(rejected)
    rec.details = {"findings": len(findings), "expectations_applied": len(applied), "rejected": len(rejected)}
    log.info("Quality agent: %d findings, %d expectations applied, %d rejected by guardrails",
             len(findings), len(applied), len(rejected))


def main() -> None:
    parser = argparse.ArgumentParser(description="Data Quality Agent")
    parser.add_argument("--dry-run", action="store_true", help="show the prompt size and cost estimate; send nothing")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    configure_logging()
    run(dry_run=args.dry_run, use_cache=not args.no_cache)


if __name__ == "__main__":
    main()
