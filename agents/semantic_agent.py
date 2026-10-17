"""Semantic Classification Agent. Maps free-text category spellings onto the fixed category taxonomy.

Runs without a human in the loop: the result is written to config/category_mapping_agent.json, which silver
uses automatically. Safety comes from guardrails rather than review: labels must come from the taxonomy (the
silver gate's allowed set, so agent output can never fail the gate), invented keys are dropped, and a run with
low coverage does not replace the last good mapping.

Cost scales with DISTINCT values, never with row count: 10 million rows still contain only a few hundred spellings,
and with --only-new a rerun sends just the spellings the current mapping does not cover.
"""
import argparse
import json
import logging
from datetime import datetime, timezone

from agents.llm_client import complete_json
from src.db import ROOT, transaction
from src.lineage import StageRecord
from src.log import configure_logging
from src.silver.clean_silver import AGENT_MAPPING_FILE, BASELINE_MAPPING_FILE, load_mapping_file
from src.silver.rules import BLANK, PLACEHOLDER, norm_key

log = logging.getLogger(__name__)

AGENT = "semantic_agent"
BATCH_SIZE = 150          # distinct values per request; keeps output well inside the token limit
RARE_THRESHOLD = 3        # only rare values get sample descriptions (swapped rows and junk live here)
SENTENCE_LEN = 25         # real labels are short; longer text is a description in the wrong column (rules.py swaps it back)
MIN_COVERAGE = 95.0       # below this the run is reported but the previous mapping stays in place


def taxonomy() -> list[str]:
    """The silver gate's allowed categories: one source of truth for what a canonical category is."""
    for e in json.loads((ROOT / "config" / "expectations.json").read_text())["silver_gate"]:
        if e["type"] == "ExpectColumnValuesToBeInSet" and e["kwargs"]["column"] == "category":
            return sorted(e["kwargs"]["value_set"])
    raise SystemExit("config/expectations.json has no category value_set in silver_gate")


def system_prompt(categories: list[str]) -> str:
    return f"""You normalize free-text maintenance-ticket categories for a facilities team.
You receive distinct raw category values (lower-cased) with how many tickets use each. Rare values also include sample
descriptions of those tickets.

Canonical categories (use these names EXACTLY, no others): {json.dumps(categories)}
"General Maintenance" is general repair work; "Other" is for miscellaneous requests that fit no trade.

Tasks:
1. Map every spelling variant or synonym to one canonical category (e.g. "lift" -> "Elevator", "a/c" -> "HVAC").
2. Some tickets have category and description SWAPPED: the category value is a sentence describing a problem, and the sample
   descriptions contain a short category label. Put such values in "swapped", mapped to the canonical category the label implies.
3. Values that are junk or carry no category information (test, asdf, ???, unknown, NULL, DELETE ME, empty) go in "unmapped".

Rules: every input value must appear in exactly one of mapping, swapped, unmapped, spelled exactly as given. Never invent values.
Respond with ONE JSON object and nothing else:
{{"mapping": {{"<value>": "<canonical>"}}, "swapped": {{"<value>": "<canonical>"}}, "unmapped": ["<value>"]}}"""


def fetch_values(only_new: bool, current: dict) -> list[dict]:
    """Distinct category values, normalised the same way silver looks them up (rules.norm_key)."""
    known = set()
    if only_new:
        known = {*current["mapping"], *current["swapped"], *current["unmapped"]} | BLANK | PLACEHOLDER
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(r"""SELECT lower(regexp_replace(btrim(category), '\s+', ' ', 'g')) v, COUNT(*) n,
                              (array_agg(DISTINCT left(description, 80)) FILTER (WHERE btrim(description) <> ''))[1:2]
                       FROM bronze.raw_tickets GROUP BY 1 ORDER BY 2 DESC, 1""")
        rows = cur.fetchall()
    out = []
    for value, n, descriptions in rows:
        if value is None or value in known or (only_new and len(value) > SENTENCE_LEN):
            continue
        item = {"value": value, "tickets": n}
        if n <= RARE_THRESHOLD and descriptions:
            item["sample_descriptions"] = descriptions
        out.append(item)
    return out


def merge(batches: list[dict], inputs: list[str], categories: list[str]) -> tuple[dict, dict, list[str], dict]:
    """Combine batch answers and apply the guardrails: known keys only, taxonomy labels only."""
    mapping, swapped, unmapped = {}, {}, []
    for b in batches:
        mapping.update(b.get("mapping", {}))
        swapped.update(b.get("swapped", {}))
        unmapped += b.get("unmapped", [])
    allowed, labels = set(inputs), set(categories)
    stats = {"hallucinated": sorted(k for k in {*mapping, *swapped, *unmapped} if k not in allowed),
             "off_taxonomy": sorted(f"{k} -> {v}" for k, v in {**mapping, **swapped}.items() if v not in labels)}
    mapping = {k: v for k, v in mapping.items() if k in allowed and v in labels}
    swapped = {k: v for k, v in swapped.items() if k in allowed and v in labels}
    unmapped = [k for k in dict.fromkeys(unmapped) if k in allowed and k not in mapping and k not in swapped]
    covered = {*mapping, *swapped, *unmapped}
    stats["missing"] = sorted(allowed - covered)
    stats["coverage_pct"] = round(100.0 * len(covered) / max(len(allowed), 1), 1)
    return mapping, swapped, unmapped, stats


def evaluate(mapping: dict, swapped: dict, items: list[dict]) -> dict:
    """Score against the hand-built baseline (config/category_mapping.json), which is kept only as a test set.

    Mapping: exact label agreement on spellings both cover. Swaps: the ticket's own description holds its real
    label, so the baseline mapping of that label is the expected answer.
    """
    baseline = load_mapping_file(BASELINE_MAPPING_FILE)["mapping"]
    shared = [k for k in mapping if k in baseline]
    disagree = sorted(f"{k}: agent {mapping[k]!r}, baseline {baseline[k]!r}" for k in shared if mapping[k] != baseline[k])
    descriptions = {i["value"]: i.get("sample_descriptions", []) for i in items}
    swap_checked, swap_wrong = 0, []
    for value, label in swapped.items():
        truth = next((baseline[norm_key(d)] for d in descriptions.get(value, []) if norm_key(d) in baseline), None)
        if truth is None:
            continue
        swap_checked += 1
        if truth != label:
            swap_wrong.append(f"{value[:60]}: agent {label!r}, ticket says {truth!r}")
    return {"mapping_compared": len(shared), "mapping_agree": len(shared) - len(disagree), "mapping_disagree": disagree,
            "swap_checked": swap_checked, "swap_agree": swap_checked - len(swap_wrong), "swap_wrong": swap_wrong}


def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "n/a"


def render(mapping, swapped, unmapped, stats, ev) -> str:
    n_inputs, calls, applied = stats["inputs"], stats["requests"], stats["applied"]
    verdict = (f"**Applied** to `{AGENT_MAPPING_FILE.relative_to(ROOT)}`; silver uses it on the next run."
               if applied else f"**Not applied**: coverage below {MIN_COVERAGE}%, the previous mapping stays in place.")
    lines = ["# Semantic Classification Agent run", "",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. {n_inputs} distinct values classified in "
             f"{calls} request(s). {verdict}", "",
             f"- Coverage: {stats['coverage_pct']}% of input values; {len(stats['missing'])} missing, "
             f"{len(stats['hallucinated'])} invented keys and {len(stats['off_taxonomy'])} off-taxonomy labels dropped.",
             f"- Mapped: {len(mapping)}; swapped-column values: {len(swapped)}; unmapped junk: {len(unmapped)}.",
             f"- Agreement with hand-built baseline: {ev['mapping_agree']}/{ev['mapping_compared']} spellings "
             f"({_pct(ev['mapping_agree'], ev['mapping_compared'])}).",
             f"- Swapped values matching the ticket's own label: {ev['swap_agree']}/{ev['swap_checked']} "
             f"({_pct(ev['swap_agree'], ev['swap_checked'])})."]
    for title, rows in (("Disagreements with baseline", ev["mapping_disagree"]), ("Wrong swaps", ev["swap_wrong"]),
                        ("Dropped by guardrails", stats["hallucinated"] + stats["off_taxonomy"]),
                        ("Missing from the answer", stats["missing"])):
        if rows:
            lines += ["", f"## {title}", ""] + [f"- {r}" for r in rows]
    lines += ["", "## Mapping", "", "| raw value | canonical |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(mapping.items(), key=lambda kv: (kv[1], kv[0]))]
    lines += ["", "## Swapped (category held a sentence)", ""] + [f"- {k[:70]} -> {v}" for k, v in sorted(swapped.items())]
    lines += ["", "## Unmapped", ""] + [f"- `{u}`" for u in unmapped] + [""]
    return "\n".join(lines)


def _write_mapping(mapping: dict, swapped: dict, unmapped: list, categories: list[str]) -> None:
    AGENT_MAPPING_FILE.write_text(json.dumps({
        "_comment": "Generated by agents.semantic_agent and used by silver automatically. Do not hand-edit; "
                    "re-run the agent (see proposals/semantic_agent_output.md for its last run).",
        "_generated": {"at": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}", "categories": categories},
        "mapping": dict(sorted(mapping.items())), "swapped": dict(sorted(swapped.items())), "unmapped": sorted(unmapped),
    }, indent=2) + "\n")


def run(rec: StageRecord | None = None, *, dry_run: bool = False, use_cache: bool = True, only_new: bool | None = None) -> None:
    """Classify, guard, apply. only_new defaults to True once an agent mapping exists (incremental mode)."""
    rec = rec or StageRecord()
    current = load_mapping_file(AGENT_MAPPING_FILE) if AGENT_MAPPING_FILE.exists() else {"mapping": {}, "swapped": {}, "unmapped": []}
    only_new = AGENT_MAPPING_FILE.exists() if only_new is None else only_new
    categories = taxonomy()
    items = fetch_values(only_new, current)
    rec.rows_in = len(items)
    rec.details = {"only_new": only_new, "requests": 0}
    if not items:
        log.info("Nothing to classify: every category value is already mapped.")
        return
    chunks = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    log.info("%d distinct values -> %d request(s) of up to %d", len(items), len(chunks), BATCH_SIZE)

    system = system_prompt(categories)
    results = [complete_json(AGENT, system, "Values (JSON):\n" + json.dumps(c, indent=1),
                             dry_run=dry_run, use_cache=use_cache) for c in chunks]
    if dry_run:
        return
    mapping, swapped, unmapped, stats = merge(results, [i["value"] for i in items], categories)
    ev = evaluate(mapping, swapped, items)
    applied = stats["coverage_pct"] >= MIN_COVERAGE
    if applied:
        if only_new:  # incremental: keep what earlier runs mapped
            mapping, swapped = {**current["mapping"], **mapping}, {**current["swapped"], **swapped}
            unmapped = sorted({*current["unmapped"], *unmapped} - mapping.keys() - swapped.keys())
        _write_mapping(mapping, swapped, unmapped, categories)
    else:
        log.warning("Semantic agent coverage %.1f%% < %.1f%%; keeping the previous mapping", stats["coverage_pct"], MIN_COVERAGE)
    stats.update(inputs=len(items), requests=len(chunks), applied=applied)
    (ROOT / "proposals" / "semantic_agent_output.md").write_text(render(mapping, swapped, unmapped, stats, ev))
    rec.rows_out = len(mapping) + len(swapped)
    rec.details.update({"requests": len(chunks), "applied": applied, "coverage_pct": stats["coverage_pct"],
                        "dropped": len(stats["hallucinated"]) + len(stats["off_taxonomy"]),
                        "baseline_agreement": f"{ev['mapping_agree']}/{ev['mapping_compared']}",
                        "swap_agreement": f"{ev['swap_agree']}/{ev['swap_checked']}"})
    log.info("Semantic agent: coverage %s%%, applied=%s, baseline agreement %s, swaps %s", stats["coverage_pct"],
             applied, rec.details["baseline_agreement"], rec.details["swap_agreement"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic Classification Agent")
    parser.add_argument("--dry-run", action="store_true", help="show request count and cost estimate; send nothing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--only-new", action="store_true", default=None, help="classify only values the current mapping lacks")
    mode.add_argument("--full", dest="only_new", action="store_false", help="reclassify every value")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    configure_logging()
    run(dry_run=args.dry_run, use_cache=not args.no_cache, only_new=args.only_new)


if __name__ == "__main__":
    main()
