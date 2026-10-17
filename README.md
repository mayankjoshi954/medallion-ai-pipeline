# Medallion Pipeline — Facility-Support Tickets

Bronze → Silver → Gold pipeline over 10,280 messy facility-support tickets, with two AI agents (Gemini, Claude or OpenAI) whose output the pipeline applies automatically.

**Contents:** [Quick start](#quick-start) · [Inputs](#inputs) · [Running the pipeline](#running-the-pipeline) · [Architecture](#architecture) · [Layer design](#layer-design) · [Generated outputs](#generated-markdown-outputs) · [AI agents](#ai-agents) · [Scaling, tradeoffs, roadmap](#what-changes-at-100) · [Testing](#testing) · [File map](#file-map)

---

## Quick start

**Prerequisites**

- Docker Desktop running
- Python 3.13 at `/opt/homebrew/bin/python3.13` (the tested version; the pinned Great Expectations declares `<3.15`, but 3.14 is untested here)
- For the agents only: an API key in `.env` (copy `.env.example`). The pipeline itself needs no key.

**Run these three commands from the project folder (fresh clone).** Once the virtual environment exists, the whole stack is one line: `docker compose up -d --wait && .venv/bin/python run.py`.

```bash
# 1. Create the virtual environment and install dependencies
/opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Start Postgres (localhost:5433, user/password "pipeline", database "tickets")
docker compose up -d --wait

# 3. Run bronze -> silver -> gold on data/raw_tickets.csv
.venv/bin/python run.py --stage all
```

Progress is logged to the terminal and to `logs/pipeline.log`; per-stage results are in the `lineage.pipeline_runs` table and alerts in `lineage.alerts`. Exit code `0` means success; `2` means a CRITICAL check blocked gold.

---

## Inputs

### Data files (`data/`)

| File | Rows | Purpose |
|---|---|---|
| `raw_tickets.csv` | 10,280 | The main dataset. Default input of every run. |
| `incremental_sample.csv` | 25 | Follow-up extract that exercises late-arriving data and schema drift: it adds `contractor_id`, drops `resolution_notes`, and has text values such as `8h` in `sla_hours`. All 25 rows are newer versions of tickets already in `raw_tickets.csv`. |

### Columns of a ticket

The 13 columns of `raw_tickets.csv`. All arrive as text and are kept as received in bronze.

| Column | Meaning | Known problems in the raw data |
|---|---|---|
| `ticket_id` | Business key, `TKT-<number>` | Some rows have `15`, `N/A`, blank or `NULL`; some IDs repeat |
| `created_at`, `resolved_at` | When the ticket was opened and closed | 7 text formats plus epoch seconds; unparseable values; some resolved before they were created |
| `category` | Type of problem (HVAC, Plumbing, ...) | 115 raw spellings (99 after lower-casing) for 11 real categories; sometimes swapped with `description` |
| `priority` | LOW / MEDIUM / HIGH / CRITICAL | 22 spellings (`lo`, `Normal`, `urgent!!!`, ...), blanks, `???` |
| `status` | Open, In Progress, Pending Vendor, Escalated, Resolved, Closed | Placeholders; status that contradicts `resolved_at` |
| `building` | Site name | Placeholders such as `unknown`, `test building` |
| `description` | Free-text problem description | Sometimes holds the category instead |
| `submitted_by`, `assigned_to` | Who raised and who handles the ticket | Non-person values such as `test`, `admin`, `system` |
| `resolution_notes` | Free text, e.g. `Duplicate of ticket #3098` | Not parsed |
| `cost` | Cost of the work | Negative sentinels (`-1`, `-999`), `error`, `TBD` |
| `sla_hours` | Target resolution time | Sentinels `0`, `-1`, `999`; real values are 4 / 8 / 24 / 48 / 72 |

The cleaning rule for each problem is in [Layer design](#layer-design).

### Configuration (`.env`)

Copy `.env.example` to `.env`. It is git-ignored.

| Variable | Used for | Expected value |
|---|---|---|
| `DATABASE_URL` | Postgres connection | `postgresql://pipeline:pipeline@localhost:5433/tickets` (also the built-in default, so it can be left out) |
| `LLM_PROVIDER` | Agent provider (optional) | `gemini`, `claude` or `openai`. If unset, the first configured key wins, in that order. |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Agent credentials | Set one. Claude can also use an `ant auth login` profile instead of a key. |
| `GEMINI_MODEL`, `CLAUDE_MODEL`, `OPENAI_MODEL` | Model per provider | Defaults: `gemini-3.1-flash-lite`, `claude-opus-5-5` (or `claude-sonnet-5-5`, `claude-haiku-5-5`), `gpt-4o-mini` |
| `CLAUDE_EFFORT` | Claude thinking depth | `low` (default), `medium`, `high` |
| `CATEGORY_MAPPING` | Silver category mapping | `agent` (default) or `baseline` to force the hand-built mapping |
| `ALERTS_ENABLED`, `SLACK_WEBHOOK_URL` | Alert delivery | `false` (default). Set `true` plus a webhook URL to post alerts to Slack. |

---

## Running the pipeline

### What a run processes

- **One CSV per run.** The default is `data/raw_tickets.csv`. Use `--file PATH` to ingest a different one. The `data/` folder is not scanned, so `incremental_sample.csv` is only loaded when you pass it.
- **Bronze** loads that one file. Rows already in bronze (same `row_hash`) are skipped, so re-running the same file inserts 0 new rows.
- **Silver and gold** do not read the CSV. They rebuild from everything in `bronze.raw_tickets`, i.e. every file loaded so far.
- **`--stage`** picks what runs: `all` (default), `bronze`, `silver` or `gold`. Silver-only and gold-only runs ignore `--file`.
- **Without `--with-agents`** no LLM is called; the pipeline uses the agents' last output (the hand-built baseline if no agent has run yet) and needs no API key.

### Command reference

| I want to... | Command |
|---|---|
| Run the whole pipeline | `.venv/bin/python run.py --stage all` |
| Run it with both AI agents refreshed first (needs an API key; cached) | `.venv/bin/python run.py --stage all --with-agents` |
| Same, but with Claude | `LLM_PROVIDER=claude .venv/bin/python run.py --stage all --with-agents` |
| Load a second file (incremental + schema drift demo) | `.venv/bin/python run.py --stage all --file data/incremental_sample.csv` |
| Re-run only one layer | `.venv/bin/python run.py --stage silver` (or `bronze`, `gold`) |
| Check a file for schema drift only | `.venv/bin/python -m src.schema.drift data/incremental_sample.csv` |
| Accept a file's schema as the new baseline | `.venv/bin/python -m src.schema.drift data/incremental_sample.csv --accept` |
| Estimate agent cost without any API call | `.venv/bin/python -m agents.quality_agent --dry-run` and `.venv/bin/python -m agents.semantic_agent --dry-run` |
| Run an agent on its own | `.venv/bin/python -m agents.semantic_agent --full` (`--full` reclassifies everything; the default is only new values; `--no-cache` skips the cache) |
| Run the unit tests | `.venv/bin/python -m pytest tests -q` |
| Wipe the database for a clean-volume run | `docker compose down -v && docker compose up -d --wait` |
| Stop Postgres but keep its data | `docker compose down` |

With `--with-agents` the order is: bronze, baseline quality check, semantic agent, quality agent, silver, quality gate, gold. A failing agent (no key, quota, bad output) records a `FAILED` stage and a WARNING alert, and the pipeline continues with the previous output.

---

## Architecture

```
CSV
 │
 ├─ [pandera drift check] ──► schema_drift_<name>.md (WARNING alert, never blocks)
 │
 ▼
BRONZE  (postgres schema: bronze)
 • All columns TEXT, loaded as-is; no type coercion on ingest
 • row_hash UNIQUE — deduplicated on re-run (INSERT … ON CONFLICT DO NOTHING)
 • source_file, source_row_number, batch_id, ingested_at lineage columns on every row
 • Unknown columns land in extra_columns JSONB
 │
 ├─ GX baseline ──────────────► measures raw quality; failures are expected
 │                               (results stored in lineage.quality_results)
 │
 ├─ [agents, with --with-agents] Gemini / Claude / OpenAI, cached, no human review
 │    • semantic agent ─► config/category_mapping_agent.json  (silver uses it)
 │    • quality agent  ─► config/expectations_agent.json      (bronze_agent checks)
 │    • a failing agent raises a WARNING; the last good output stays in use
 ▼
SILVER  (postgres schema: silver)
 • bronze → latest version per ticket → typed + cleaned + flagged (rules.py pure functions)
 • category from the agent mapping; category_source says agent / agent_swap / rule_swap / baseline
 • DLQ: silver.rejected_rows  (only rows that can't be keyed or placed on a timeline)
 │
 ├─ GX gate ──────────────────► CRITICAL failure, or > 30% DLQ share (of this run's new rows
 │                               or of all bronze), blocks gold
 │                               (gold gets a SKIPPED row, exit code 2;
 │                               a later --stage gold run is blocked too)
 ▼
GOLD  (postgres schema: gold)
 • category_sla_performance  — SLA breach rates by category × priority
 • assignee_performance      — resolution speed and cost by assignee
 • open_ticket_backlog       — open tickets bucketed by building × priority × age
 • TRUNCATE + INSERT in one transaction; deterministic, reproducible

cross-cutting
 lineage.*    — pipeline_runs (one row per stage, agents included: SUCCESS / FAILED / SKIPPED),
                quality_results, alerts, agent_runs (tokens, cost, cache hits per LLM call)
 reports/     — schema drift reports (Markdown), written when a file's schema differs or is checked
```

**Tech choices:** plain Python CLI (`run.py`), Postgres 16 in Docker (one schema per layer, `bronze` / `silver` / `gold`, plus `lineage`, so layers are separated by namespace and could get separate permissions), Great Expectations for data-quality gates, pandera for schema drift. No dbt, Airflow or Alembic — intentional for a 10 k-row batch (see [Tradeoffs](#tradeoffs)).

---

## Layer design

### Bronze — land everything, change nothing

Every row from the CSV is written as TEXT. Type errors, encoding quirks and schema drift are all preserved so they can be audited later. `row_hash` (SHA-256 of the raw values) makes re-runs safe: re-ingesting the same file inserts zero new rows. Each file load is also summarised in `bronze.ingestion_log` (rows in file, new, skipped).

Unknown new columns (e.g. `contractor_id` in the incremental file) land in `extra_columns` JSONB. Pandera detects them, reports a diff, and fires a WARNING alert. No table is altered automatically; a human applies the suggested `ALTER TABLE`, then runs `python -m src.schema.drift <file> --accept` to update the snapshot. Until then the drift alert fires on every run of that file.

### Silver — clean, type, flag and keep

Policy: **clean, flag and keep**. A row is only rejected to the DLQ when it is structurally unusable (no parseable `ticket_id` or `created_at`). Everything else is repaired, flagged, and promoted. Analysts can see and filter on data quality in gold rather than losing rows silently.

**Latest version wins.** If bronze holds several versions of one `ticket_id` (for example the incremental file), only the most recently ingested one is promoted; older versions stay in bronze and are counted as "superseded" in the log.

Every rule lives in `src/silver/rules.py` as a pure function with no database I/O, so it is unit-testable in isolation (53 tests, see [testing_report.md](testing_report.md)).

#### Cleaning rules

| Column | Rule | Flag raised | Why |
|---|---|---|---|
| `created_at`, `resolved_at` | 7 text formats plus epoch seconds; year must be 2000–2100 | `created_at_invalid`, `resolved_at_invalid` | Many formats in one column; a naive cast would corrupt or drop rows |
| `resolved_at` vs `created_at` | Flag when resolved < created | `resolved_before_created` | 1,866 rows (18%); kept, but excluded from resolution-time metrics |
| `status` vs `resolved_at` | Flag open ticket with timestamp or done ticket without | `status_resolved_at_mismatch` | 2,227 rows (22%); common in migrated data |
| `category` / `description` | Swap back when the semantic agent marked the sentence as swapped, or when description is a known category key | `category_swap_corrected` | 49 rows had columns exchanged at source |
| `category` | Map to canonical via `config/category_mapping_agent.json` (agent-written; hand-built `category_mapping.json` is the fallback) | `category_unmapped`, `category_placeholder` | 115 raw spellings collapsed to 11 canonical groups; `category_source` records which mapping/swap applied |
| `priority` | Map to `LOW/MEDIUM/HIGH/CRITICAL` | `priority_placeholder`, `priority_inferred` | 22 spellings; `urgent!!!` / `ASAP` → CRITICAL with flag |
| `status` | Map to 6 canonical states | `status_placeholder` | Junk values vanish from backlog otherwise |
| `cost` | Parse numeric (`$` and commas allowed); null negatives | `cost_sentinel`, `cost_unparseable` | Negative sentinels (-1, -999) must not be summed |
| `sla_hours` | Accept 1–720 h (a trailing `h`/`hrs` is stripped); null 0, -1, 999 | `sla_out_of_range` | Only 5 real tiers exist; sentinels corrupt breach-rate math |
| `building`, `submitted_by` | Null placeholder tokens | `building_placeholder`, `submitted_by_placeholder` | Fake sites pollute location rollups |

The flag counts quoted above come from a full run on the 10,280-row file with the baseline mapping; see [testing_report.md](testing_report.md).

#### DLQ policy

Only 2 reject criteria:
1. `ticket_id` does not match `TKT-\d+` (can't be keyed)
2. `created_at` matches no known format (can't be placed on a timeline)

Result on the main file: 30 rejected rows (26 bad IDs, 4 missing timestamps). The raw payload is stored in `silver.rejected_rows` for later reprocessing.

### Gold — three business-facing aggregates

| Table | Grain | Purpose |
|---|---|---|
| `category_sla_performance` | category × priority | Breach rates, avg and p90 resolution time. Answers: *which category is slowest?* |
| `assignee_performance` | assigned_to | Ticket volume, escalation rate, avg cost per assignee. Identifies under-resourced technicians. |
| `open_ticket_backlog` | building × priority × age-bucket | Snapshot of outstanding work. Age is measured against `MAX(created_at)`, not `now()`, so the report is reproducible. |

TRUNCATE + INSERT runs in a single transaction, so re-running changes no counts.

---

## Generated Markdown Outputs

Everything in `proposals/` and `reports/` is generated and overwritten on each run. The repo includes the files from one live run (`proposals/*_output.md` and the drift report for `raw_tickets.csv`) as samples; running again replaces them. Run results themselves live in the database (`lineage.*`) and the log.

### `proposals/`: what the LLM agents did

| File | What it is | How it is populated | What it contains | Use |
|---|---|---|---|---|
| `quality_agent_output.md` | Run log of the Data Quality Agent | `agents/quality_agent.py` profiles bronze, asks the LLM for findings, then writes this file | Findings (column, severity, evidence, why it matters, rule, suggested SQL/Python), plus how many expectations the guardrails accepted or rejected | Review what the agent suggested. Only the accepted expectations are applied (to `config/expectations_agent.json`); the SQL/Python is shown but never executed |
| `semantic_agent_output.md` | Run log of the Semantic Classification Agent | `agents/semantic_agent.py` classifies the distinct raw category values, then writes this file | Coverage stats, agreement with the hand-built baseline, and the raw value to canonical category table | Audit the mapping silver will use (applied to `config/category_mapping_agent.json`) |

A `--dry-run` of the quality agent also writes the prompt it would send to `proposals/quality_agent_prompt_preview.txt`, so you can check what data would leave the machine.

### `reports/`: schema drift

| File | What it is | How it is populated | What it contains | Use |
|---|---|---|---|---|
| `schema_drift_<file>.md` | Schema comparison of one input file against `schema_snapshots/raw_tickets_schema.json` | `src/schema/drift.py` (pandera), on every bronze run for the file being loaded, or via the standalone drift command | "No drift detected", or added/removed columns, type changes and a proposed `ALTER TABLE` | Human review. Nothing is applied automatically; bronze keeps unknown columns in `extra_columns` so no data is lost |

---

## AI agents

### Selection rationale

Four candidate agent types were considered. Ranked by pain relief on this dataset:

| Rank | Agent | Why chosen / not chosen |
|---|---|---|
| 1 | **Data Quality Agent** | Every new dataset needs a first-pass profile. The LLM explains *why* a field matters, not just that it has nulls. Chosen. |
| 2 | **Semantic Classification Agent** | Worst source problem is free-text category chaos (115 spellings, swapped columns). Distinct-value scaling means 1 LLM request covers all 10 k rows. Chosen. |
| 3 | Schema Inference / Evolution | Solved deterministically by pandera + JSON diff. No LLM needed. Not built. |
| 4 | Gold Design Agent | 2–3 tables needs business judgment, not a language model. Gold was designed manually. Not built. |

### How they run

**Agents run without a human in the loop.** `run.py --with-agents` runs both agents after bronze and before silver, and their output is applied automatically:

| Agent | Writes | Used by | Never automated |
|---|---|---|---|
| Semantic | `config/category_mapping_agent.json` (mapping + swapped sentences) | silver `category`, `category_source` | — |
| Quality | `config/expectations_agent.json` (GX expectations) | `bronze_agent` suite on every run (measurement only) | executing the SQL/Python it writes for each finding |

They can also run on their own (`python -m agents.<name>`). Each run writes a readable report to `proposals/`.

Without review, safety comes from guardrails:
- **Fixed taxonomy.** The semantic agent may only use the 11 categories in the silver gate's allowed set, so its output can never fail the gate.
- **Drops instead of trust.** Invented keys and off-taxonomy labels are dropped and counted.
- **Coverage floor.** A run that covers fewer than 95% of values does not replace the last good mapping.
- **Validated before use.** Expectations must be allow-listed types on known columns, and GX must accept their arguments before they are written. They only measure bronze and can never block a stage.
- **Failures don't stop the pipeline.** See [Running the pipeline](#running-the-pipeline).
- **Override.** `CATEGORY_MAPPING=baseline` switches silver back to the hand-built mapping.

The one thing deliberately left manual: the generated cleaning code. Running LLM-written SQL or Python unattended is the highest-risk step, and every sample so far needed rewriting (see below). The live rules stay in `src/silver/rules.py`.

### Providers and prompt design

Both agents share one client (`agents/llm_client.py`) with three providers. Set one key; `LLM_PROVIDER` picks explicitly.

| Provider | SDK | Default model | Notes |
|---|---|---|---|
| Gemini | `openai` (Gemini's OpenAI-compatible endpoint) | `gemini-3.1-flash-lite` | `temperature=0`, `response_format=json_object` |
| OpenAI | `openai` | `gpt-4o-mini` | same |
| Claude | `anthropic` (`agents/claude_client.py`) | `claude-opus-5-5` (`CLAUDE_MODEL`; `claude-sonnet-5-5` / `claude-haiku-5-5` cost less) | Current Claude models always think and don't accept `temperature`; `effort` (`CLAUDE_EFFORT`, default `low`) sets depth, the cache provides repeatability. `max_tokens` is at least 16k because thinking counts towards it. The server-side refusal fallback (`fallbacks: "default"`) is enabled on models that support it. No `base_url` is needed; the SDK uses Anthropic's API (override with `ANTHROPIC_BASE_URL` if you use a gateway). Run live for the Assessment below (Opus 5.5); the dry-run, missing-credentials and JSON-parsing paths are also tested. |

- **Context given:** quality agent gets a column profile (blank %, cardinality, top values and shapes, numeric percentiles and IQR outliers). Semantic agent gets distinct category values with counts, up to two sample descriptions for rare values, and the fixed taxonomy. No few-shot examples; the hand-built mapping is only used to score the output.
- **Stable prompts:** every profile query breaks ties in its `ORDER BY`, so identical data gives a byte-identical prompt and a cache hit on any database.
- **Untrusted text in the prompt:** both prompts contain raw data (cell values, ticket descriptions), so prompt injection through the data is possible. Now that output is applied unattended, the guardrails carry this. A manipulated answer can only pick a taxonomy label for a value that really exists, or add a non-blocking bronze check. It cannot add categories, change rules, or run code.
- **JSON-only output:** the system prompt fixes the exact shape; Gemini and OpenAI also use JSON mode. Every answer is validated against the shape before use.
- **Output caps:** `max_output_tokens=3500` (Gemini/OpenAI), at least 16k for Claude.
- **Guardrails:** quality agent: Python must pass `ast.parse`, SQL is rejected if it contains DDL/DML keywords, columns and expectation types are allow-listed, and GX must accept the arguments. Semantic agent: known keys only, taxonomy labels only, 95% coverage floor. Everything dropped is listed in the run report.
- **Caching:** responses keyed by `(model, prompt_hash)` in `.agent_cache/` (git-ignored).
- **Cost:** `--dry-run` estimates input tokens and worst-case cost with no network call, and says when the answer is already cached. Dollar cost is computed for models in the `PRICES` table (OpenAI and Claude). Gemini prices change often and are not hard-coded, so Gemini runs show `$unknown` and `estimated_cost_usd` is NULL; tokens are still tracked. The dry-run estimate is a worst case (it assumes the full output cap, 16k tokens on Claude): about $0.33 per agent run on Opus 5.5 and about $0.008 on Haiku 5.5. The live Opus run actually cost $0.057 (semantic) and $0.108 (quality).
- **Logging:** every call (hit or miss, tokens, latency, status) goes to `lineage.agent_runs`; with `--with-agents` each agent is also a stage in `lineage.pipeline_runs`.

### Assessment

> Figures are from the committed live run: both agents on Claude Opus 5.5 (`claude-opus-5-5`, effort `low`) via `run.py --stage all --with-agents` on 2026-10-17. Tokens and cost are from `lineage.agent_runs`; the full outputs are in `proposals/`.

| | Requests | Tokens in / out | Estimated cost | Time |
|---|---|---|---|---|
| Semantic agent | 1 | 4,961 / 1,858 | $0.057 | 15 s |
| Quality agent | 1 | 6,210 / 4,158 | $0.108 | 33 s |

A second run on the same data reuses `.agent_cache/` and costs 0 tokens.

#### Data Quality Agent

**Sample input** (profile excerpt; `numeric_distribution` adds percentiles and IQR outliers):
```json
{
  "column": "sla_hours",
  "numeric_distribution": {"min": -1.0, "p25": 8.0, "median": 24.0, "p75": 48.0, "p99": 999.0, "max": 999.0,
                           "iqr_outliers": 557, "most_common_outlier_values": {"999": 557}}
}
```

**Sample output** (finding 2 in `proposals/quality_agent_output.md`; shown, never executed):
```python
# sla_hours: sentinel 999 (557 rows), plus 0, -1, N/A, NULL: null out (critical)
def clean_sla(v):
    s = (v or '').strip()
    return int(s) if s in {'4', '8', '24', '48', '72'} else None
```

**What it produced:** 11 findings (ticket IDs, `sla_hours`, `cost`, both date columns, a cross-column status/`resolved_at` mismatch, priority, status, building, submitter, resolution notes) and 8 expectations. The guardrails rejected nothing.

**What was applied automatically:** the 8 expectations, as the `bronze_agent` suite. Six of the eight fail on the raw data, which is the point: they measure how dirty bronze is and never block a stage. For example the ticket-ID regex fails on 26 rows (the same 26 bad IDs the DLQ rejects), and the `sla_hours` allowed-values check fails on about 40% of rows (blanks, `N/A` and the sentinels).

**What it got wrong** (reported, never executed):

| Finding | Problem |
|---|---|
| `created_at`: parser tries `%d-%m-%Y %H:%M:%S` | The dash-separated dates in this file are month-first (`%m-%d-%Y`; the first field never exceeds 12). The agent noticed the day/month ambiguity but guessed the wrong order, so its code would swap day and month for those rows. `rules.py` uses month-first. |
| `sla_hours`: keep only 4/8/24/48/72 | Sentinels are handled correctly, but silver accepts any value from 1 to 720 so a legitimate new tier is not discarded. |
| `cost`: "flag 0 as possibly unknown" | `0` is a valid cost, so `rules.parse_cost()` keeps it unflagged. |
| `cross-column`: rule also flags `resolved_at` before `created_at` | The profile has no evidence for that case, so it is a guess. Silver does flag it (`resolved_before_created`, 1,866 rows). |

**What it still cannot see:** the category/description swap (the semantic agent handles it; the quality prompt tells it to skip categories) and the exact counts of the cross-column issues, which a per-column profile only hints at.

#### Semantic Classification Agent

**Cost scaling:** cost scales with *distinct values*, not rows. 99 distinct values → 1 request whether there are 10 k or 10 M rows. Incremental mode (the default once a mapping exists) sends only spellings the current mapping lacks, and responses are cached by (model, prompt) hash.

**Sample input** (from the 99 distinct values sent; rare values carry sample descriptions):
```
{"value": "hvac", "tickets": 1047}, {"value": "a/c", ...}, {"value": "heating/cooling", ...},
{"value": "cold", "tickets": 1, "sample_descriptions": ["HVAC"]}, ...
```

**Sample output** (`config/category_mapping_agent.json`):
```
a/c             → HVAC
heating/cooling → HVAC
misc            → Other
lift            → Elevator
cold            → HVAC         ← swapped: the category column held the description
```

**Scored automatically on every run** against the hand-built `config/category_mapping.json`, which is kept as a test set and fallback, not the live config:
- Mapping agreement: **46/46** spellings.
- Swapped sentences matching the ticket's own label: **46/46**.
- Coverage 100%; 0 invented keys or off-taxonomy labels dropped; 46 mapped, 46 swapped values and 7 junk values left unmapped.

**In silver:** `category_source` shows where each category came from: `agent` 10,200 rows, `agent_swap` 49, NULL 1 (no category). Category counts and all three gold tables are identical to the hand-built mapping (56 / 8 / 304 rows).

**What made it work unattended:** giving it the fixed taxonomy (with one line separating "General Maintenance" from "Other") instead of letting it invent groups.

---

## What Changes at 100×

At 100× volume (~1 M rows, daily batch):

| Concern | Change needed |
|---|---|
| **Bronze ingest** | Replace `INSERT … ON CONFLICT` row-by-row with `COPY` + server-side staging table. Add range partitioning on `ingested_at`. |
| **Silver transform** | Use an incremental `MERGE` (upsert) instead of full TRUNCATE + INSERT. Keep a `_version` counter to handle late-arriving updates. |
| **GX** | Switch from pandas DataFrames to a GX SQL datasource so expectations run inside Postgres, not in Python memory. |
| **Schema drift** | Move pandera snapshot to a proper schema registry (e.g. AWS Glue Schema Registry). |
| **Semantic agent** | Distinct-value count grows slowly; the cost model holds. Add batching for >500 values and streaming cache checks. |
| **Gold** | Move to a proper analytical warehouse (BigQuery, Redshift, Snowflake) or at minimum Postgres partitioned materialized views. |
| **Orchestration** | Airflow (or Prefect) for retry, backfill and cross-DAG dependencies. |
| **Observability** | Turn on Slack delivery (`ALERTS_ENABLED=true`) with retry and per-metric rate limiting, Prometheus metrics, PagerDuty for critical alerts. |
| **Late-arriving data** | Define a replay window; the incremental file demo (`data/incremental_sample.csv`) already exercises the superseded-version path. |

---

## Tradeoffs

| Decision | Why | Downside |
|---|---|---|
| Plain Python CLI, no Airflow | No infra overhead for a 10 k-row batch | No retries, no UI, no cross-pipeline dependencies |
| Full TRUNCATE + INSERT for silver/gold | Guaranteed idempotency, simple code | Rewrites entire table; won't scale past ~10 M rows |
| Pandera + JSON snapshot for drift | Zero infra, readable diffs | Manual `ALTER TABLE`; no automatic migration |
| Agent output applied without review | No manual promotion step; agents keep up with new spellings | Correctness rests on guardrails (fixed taxonomy, allow-lists, coverage floor) rather than a person; generated code stays unautomated |
| Agents opt-in (`--with-agents`) | Default pipeline is deterministic, free, works without an API key | Output is only as fresh as the last agent run |
| No dbt | Simpler dependency graph | SQL transforms are embedded Python strings, not version-controlled SQL files with a test harness |

---

## Production Roadmap

- **dbt** — move SQL transforms out of Python strings into version-controlled `.sql` files with a test harness
- **Airflow / Prefect** — retries, backfill, cross-pipeline dependencies, SLA alerting
- **Alembic / Flyway** — managed schema migrations instead of idempotent DDL re-runs
- **OpenLineage / Marquez** — column-level lineage graph visible to all teams
- **Alert delivery** — Slack posting in `src/observability.py` is off by default (`ALERTS_ENABLED`); add retries, rate limiting, Prometheus + PagerDuty
- **PII / sensitivity tagging** — tag `submitted_by`, `description`, `resolution_notes` before they leave the warehouse
- **`reprocess_dlq()`** — implement the stub in `src/silver/dlq.py` (currently raises `NotImplementedError`) to replay rejected rows after upstream fixes
- **`submitted_by` entity resolution** — "Joe (in-house)", "joe smith", "J. Smith" are probably the same person
- **Duplicate-ticket detection** — `resolution_notes` contains "Duplicate of ticket #…" strings; not parsed

### Explicitly not built

These were conscious cuts, not oversights.

- **Cross-source reconciliation** — single source dataset (N/A)
- **Full agent evaluation harness** — only a light version exists: each semantic-agent run is scored against the hand-built mapping (mapping and swap agreement, coverage, dropped keys) in its report. There is no fixed test set across prompt or model changes, and no scoring for the quality agent.
- **`lineage.transformations` (row-level)** — row lineage is carried by `source_row_hash`, `silver_run_id`, `gold_run_id` columns already on every table
- **dbt, Airflow, Alembic** — over-engineering for a 10 k-row batch pipeline

---

## Testing

Unit tests: `.venv/bin/python -m pytest tests -q`. Test scenarios, results and how to repeat them are in **[testing_report.md](testing_report.md)**.

---

## File map

```
run.py                  CLI entry point
docker-compose.yml      Postgres 16 (port 5433)
requirements.txt        pinned dependencies
.env.example            configuration template (copy to .env)
data/
  raw_tickets.csv       main input, 10,280 rows
  incremental_sample.csv  25-row incremental/drift demo
src/
  pipeline.py           stage orchestration
  db.py                 connection helper, transaction scope, DDL runner
  log.py                logging (stdout + logs/pipeline.log)
  lineage.py            lineage table writers
  observability.py      alert evaluation + Slack delivery (off by default)
  bronze/load_bronze.py ingest CSV → bronze
  silver/
    rules.py            pure cleaning functions (unit-tested)
    dlq.py              keep/reject policy
    clean_silver.py     orchestrates bronze → silver
  gold/build_gold.py    3 gold aggregates
  quality/run_gx.py     Great Expectations runner
  schema/drift.py       pandera drift checker
sql/                    idempotent DDL (applied on every run)
config/
  expectations.json     GX suites: bronze_baseline + silver_gate (hand-written)
  expectations_agent.json  bronze_agent suite, written by the quality agent (created by an agent run)
  category_mapping_agent.json  live category mapping, written by the semantic agent (created by an agent run)
  category_mapping.json hand-built mapping: fallback and the agent's test set
agents/
  llm_client.py         shared LLM wrapper: Gemini / OpenAI (openai SDK), cache, dry-run, logging
  claude_client.py      Claude provider (anthropic SDK)
  quality_agent.py      data-quality profiling agent
  semantic_agent.py     category normalisation agent
proposals/              per-agent-run reports (what was applied, dropped, how it scored)
reports/                schema drift reports
schema_snapshots/       pandera baseline for drift detection
logs/                   pipeline.log (git-ignored)
tests/                  61 unit tests (rules, header-only drift, agent guardrails)
testing_report.md       test scenarios, results and how to repeat them
```
