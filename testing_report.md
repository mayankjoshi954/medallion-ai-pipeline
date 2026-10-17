# Testing Report

How the pipeline was tested, what was found, and how to repeat it. Back to the [README](README.md).

Last verified: 2026-10-17, on Python 3.13, Postgres 16 (Docker), dependencies from `requirements.txt`. The end-to-end runs used a scratch database (`tickets_verify`, dropped afterwards), so the normal `tickets` database was not touched.

## Summary

| Area | Result |
|---|---|
| Unit tests | 61 passed, 0 failed (about 2 s) |
| End-to-end: full run on `raw_tickets.csv` | Passed: 10,280 bronze, 10,250 silver, 30 DLQ, gold 56 / 8 / 304 rows |
| End-to-end: incremental file | Passed: 25 new bronze rows, drift WARNING, silver and gold unchanged |
| End-to-end: idempotent re-run | Passed: 0 new bronze rows, all counts unchanged |
| Failure input: header-only CSV | Passed: `empty_source` WARNING, earlier loads still rebuilt, exit 0 |
| Failure input: 3,000 junk rows | Passed: CRITICAL alert, gold blocked, exit 2 |
| Agents: dry-run | Passed: no network call, cost estimate printed |
| Agents: live LLM calls | Passed, in a run made by the project owner (Claude Opus 5.5); results read back from the database, not re-run by this report |

## 1. Unit tests

Run: `.venv/bin/python -m pytest tests -q` (no database or API key needed).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_rules.py` | 53 | Silver cleaning rules and DLQ policy, using real rows from `data/raw_tickets.csv` |
| `tests/test_agents.py` | 6 | Semantic-agent guardrails and mapping files, Claude JSON parsing |
| `tests/test_drift.py` | 2 | Schema drift on a header-only file |

`test_rules.py` in detail:

| Rule | Tests | Cases checked |
|---|---|---|
| Timestamps | 14 | All 7 text formats plus epoch seconds parse correctly; garbage (`not a date`, `TBD`, `asap`, `00/00/0000`, `???`) is flagged invalid; blanks are missing, not invalid |
| Cost | 10 | `$3065.86`, plain numbers and `0` kept; `-1` and `-999` flagged `cost_sentinel`; `error` flagged `cost_unparseable`; `TBD`, `N/A` and blank become NULL without a flag |
| SLA hours | 10 | `4` and `48` kept; `999`, `0`, `-1` and `0h` flagged `sla_out_of_range`; `48h` and `8 hrs` accepted; blanks are NULL |
| Priority | 8 | `lo`, `med`, `Normal`, `hi`, `crit` and upper-case forms map correctly; free text (`urgent!!!`) and junk are handled and flagged |
| Status | 1 | Spelling variants map to the 6 canonical states; junk is flagged |
| Category | 3 | Variants collapse to one canonical value; swapped category/description is restored; unknown categories are flagged, not guessed |
| Resolution order | 1 | `resolved_before_created` is raised |
| Ticket ID / DLQ | 5 | `15`, `N/A`, blank and `NULL` go to the DLQ; `TKT-` IDs pass |
| Row hash | 1 | Stable for the same content, changes when content changes |

`test_agents.py` checks that invented keys and off-taxonomy labels are dropped, that agent-detected swaps are applied and sourced correctly, that the rule-based swap still works without agent swaps, that `category_source` is right for plain and empty categories, that both mapping file formats load, and that Claude output wrapped in a code fence still parses.

`test_drift.py` checks that a header-only file with the known columns reports no drift, and that a header-only file with different columns still reports the added and removed ones.

## 2. End-to-end runs

Each scenario ran against a fresh scratch database in this order, using the commands in the README.

### 2.1 Full run: `run.py --stage all`

Exit code 0.

| Stage | Result |
|---|---|
| Bronze | 10,280 rows read, 10,280 inserted, 0 skipped |
| GX bronze baseline | 1 of 13 expectations passed; 10,199 rows (99.2%) fail at least one check; 30 rows (0.29%) fail a blocking check |
| Silver | 10,250 tickets promoted; 30 to the DLQ (26 `invalid_ticket_id`, 4 `missing_created_at`); 0 superseded versions |
| GX silver gate | 10 of 12 passed, 0 blocking failures |
| Gold | `category_sla_performance` 56 rows, `assignee_performance` 8 rows, `open_ticket_backlog` 304 rows |

The 30 blocking rows counted by Great Expectations match the 30 DLQ rows exactly.

Silver data-quality flags after this run (counted from `silver.tickets.data_quality_flags`):

| Flag | Tickets | % of silver |
|---|---|---|
| `status_resolved_at_mismatch` | 2,227 | 21.73 |
| `resolved_before_created` | 1,866 | 18.20 |
| `sla_out_of_range` | 1,041 | 10.16 |
| `cost_sentinel` | 192 | 1.87 |
| `category_swap_corrected` | 49 | 0.48 |

Silver used the hand-built baseline mapping (46 spellings) because no agent mapping file was present.

### 2.2 Incremental file: `--file data/incremental_sample.csv`

Exit code 0.

- A `schema_drift` WARNING fired and `reports/schema_drift_incremental_sample.md` was written (new `contractor_id`, removed `resolution_notes`, type changes on `cost` and `sla_hours`).
- Bronze read 25 rows and inserted 25, giving 10,305 in total. `contractor_id` is kept in `extra_columns`.
- Silver: 10,250 tickets, unchanged. All 25 rows are newer versions of existing tickets, so the log shows "25 superseded versions dropped".
- Gold: counts unchanged (56 / 8 / 304).

### 2.3 Idempotency: `--stage all` again

Exit code 0. Bronze read 10,280 rows, inserted 0 and skipped 10,280. Silver and gold counts were identical to the previous run.

### 2.4 Failure inputs

| Input | Command | Observed | Exit |
|---|---|---|---|
| Header-only CSV (13 column names, no rows) | `run.py --stage all --file header_only.csv` | `empty_source` WARNING ("silver and gold reflect earlier loads only"); 0 rows read; silver rebuilt unchanged from earlier loads | 0 |
| 3,000 rows with invalid ticket IDs, on top of the 10,305 already loaded | `run.py --stage all --file junk3000.csv` | 3,000 inserted; 3,026 `invalid_ticket_id` rows in the DLQ; CRITICAL alert "100.0% of this run's new rows went to the DLQ (limit 30%)"; gold skipped | 2 |
| Normal run straight after the junk load | `run.py --stage all` | Only a WARNING: 22.8% of all bronze rows are in the DLQ (warn above 5%); gold rebuilt | 0 |
| Gold-only run after that | `run.py --stage gold` | Gold rebuilt, no alert | 0 |

The junk and header-only files were generated in a temporary folder outside the project and deleted afterwards.

### 2.5 Agents

**Dry-run (no network).** `LLM_PROVIDER=claude python -m agents.<name> --dry-run` printed these worst-case estimates and sent nothing. The worst case assumes the full output cap (16k tokens on Claude):

| Agent | Input tokens | Worst-case cost, Opus 5.5 | Haiku 5.5 |
|---|---|---|---|
| Quality | about 2,836 | $0.331 | $0.0083 |
| Semantic (`--full`) | about 2,577 | $0.330 | not run |

This check found and fixed a bug: the estimate used a 3,500-token output cap for Claude, so the "worst case" ($0.08) was lower than the real cost of the quality run ($0.108).

**Live run.** `run.py --stage all --with-agents` was run on 2026-10-17 with Claude Opus 5.5 (effort `low`) by the project owner. Read back from `lineage.agent_runs`, `lineage.pipeline_runs` and the files in `proposals/`:

| Check | Result |
|---|---|
| Pipeline | All 8 stages `SUCCESS`; silver 10,250 + 30 DLQ; gold 368 rows |
| Semantic agent | 1 request, 4,961 in / 1,858 out tokens, $0.057, 15 s; coverage 100%; 46/46 spellings and 46/46 swaps agree with the hand-built mapping; 0 dropped |
| Silver `category_source` | `agent` 10,200, `agent_swap` 49, NULL 1 |
| Quality agent | 1 request, 6,210 in / 4,158 out tokens, $0.108, 33 s; 11 findings; 8 expectations accepted, 0 rejected |
| `bronze_agent` suite | 2 of 8 expectations pass on raw data (measurement only, never blocks) |

Reviewing the quality agent's output against the code found one real mistake in its suggested code (month/day order for dash-separated dates); it is reported and never executed. See the README's Assessment.

## 3. Not covered

- **Repeat of the live agent run.** The live run above was made once, by the project owner, and was not repeated for this report. A second run on the same data should be a cache hit (0 tokens); that is by design and was not re-checked here.
- **Other providers.** Gemini and OpenAI were exercised only in earlier development runs; this report did not repeat them.
- **Slack alert delivery.** Disabled by default (`ALERTS_ENABLED`); alerts are verified only as rows in `lineage.alerts`.
- **Header-only file on an empty database.** The README states that silver's row-count gate fails and gold is blocked in that case. It was checked when written, but was not repeated for this report.
- **Volume.** Only the 10,280-row dataset was run. See "What Changes at 100x" in the README.

## 4. How to repeat

```bash
# Unit tests
.venv/bin/python -m pytest tests -q

# End-to-end, on a scratch database so your normal data is untouched
docker compose up -d --wait
docker compose exec -T postgres psql -U pipeline -d postgres -c "CREATE DATABASE tickets_verify"
export DATABASE_URL=postgresql://pipeline:pipeline@localhost:5433/tickets_verify
.venv/bin/python run.py --stage all                                    # 2.1
.venv/bin/python run.py --stage all --file data/incremental_sample.csv # 2.2
.venv/bin/python run.py --stage all                                    # 2.3
docker compose exec -T postgres psql -U pipeline -d postgres -c "DROP DATABASE tickets_verify"
unset DATABASE_URL
```

A run writes `logs/pipeline.log` and, for each file bronze loads, `reports/schema_drift_<file>.md`; delete them if you want a clean tree.
