# Data Quality Agent run

Generated 2026-10-17 12:41 UTC from a profile of 10280 bronze rows.

11 findings, 8 expectations accepted by guardrails, 0 rejected.

- **Applied automatically:** the 8 accepted expectations, written to `config/expectations_agent.json` and run against bronze on every pipeline run (suite `bronze_agent`).
- **Not executed:** the SQL/Python below. It is shown as the agent wrote it; the live cleaning rules are in `src/silver/rules.py`.

## 1. `ticket_id`: Placeholder/invalid IDs and non-unique keys (critical, action: reject_to_dlq)

- **Evidence:** distinct_values 10261 vs row_count 10280. Placeholders: 'N/A' 9, '' 8, 'NULL' 5. Numeric-only IDs: shape '99' 4 (e.g. '15','20','90','97'). Valid shapes are 'aaa-9999' 9000 and 'aaa-99999' 1254.
- **Why it matters:** Without a reliable primary key, tickets cannot be deduplicated or joined to downstream facts. Backlog and SLA counts will be inflated or merged incorrectly.
- **Rule:** ticket_id must match ^TKT-\d{4,5}$. Rows with blank, placeholder or bare-numeric IDs go to the DLQ.

```sql
SELECT *, CASE WHEN ticket_id ~ '^TKT-[0-9]{4,5}$' THEN 'ok' ELSE 'dlq' END AS ticket_id_check FROM bronze_tickets
```

## 2. `sla_hours`: Sentinel values 999, 0, -1 and N/A/NULL text (critical, action: null_out)

- **Evidence:** Valid values are 48:1267, 24:1252, 8:1238, 4:1194, 72:1193. '999' appears 557 times and is the only IQR outlier (557); p99=max=999. '0' 484, '-1' 9, 'N/A' 1032, 'NULL' 14, blank 2040.
- **Why it matters:** 999 is a placeholder sentinel, not a real SLA. Treating it as real would make breaches look rare, and treating 0/-1 as real would make every ticket look breached, skewing SLA compliance reporting.
- **Rule:** Keep only values in {4,8,24,48,72}. Null out 999, 0, -1, N/A, NULL and blanks.

```python
def clean_sla(v):
    s = (v or '').strip()
    return int(s) if s in {'4', '8', '24', '48', '72'} else None
```

## 3. `cost`: Sentinel negatives/zero, text placeholders and currency symbols (critical, action: normalize)

- **Evidence:** '-1' 192, '-999' 3, negative_values 195, '0' 295. Text placeholders: 'N/A' 494, 'TBD' 484, 'error' 12, 'NULL' 6, blank 2637. Currency-prefixed shapes: '$9999.99' 724, '$999.99' 156. min -999, p01 -1, median 6038.995, max 14996.24, iqr_outliers 0.
- **Why it matters:** -1 and -999 are placeholder sentinels that would reduce cost totals, and '$'-prefixed values are lost if the column is cast naively. Either error distorts maintenance spend reporting.
- **Rule:** Strip a leading '$' and cast to numeric. Null out negatives and the text placeholders. Flag 0 as possibly unknown.

```python
def clean_cost(v):
    s = (v or '').strip().lstrip('$')
    try:
        x = float(s)
    except ValueError:
        return None
    return x if x >= 0 else None
```

## 4. `created_at`: Eight mixed date formats plus placeholder values (critical, action: reject_to_dlq)

- **Evidence:** Formats seen: 'T' ISO 2625, space ISO 2020, 'MM/DD/YYYY hh:mm am' 1601, date-only 1158, 'DD-Mon-YYYY' 849, 'MM/DD/YYYY' 806, 'DD-MM-YYYY' 698, epoch 501. Placeholders: '00/00/0000' 8, blank 7, text shapes 'aaaa' 6 / 'aaa' 5 / 'aaa a aaaa' 4.
- **Why it matters:** An unparseable creation date makes ticket age, backlog aging and SLA clock start impossible to compute. The ambiguous dd-mm vs mm/dd formats can silently swap day and month.
- **Rule:** Parse each known format explicitly, including epoch seconds. Send unparseable values and '00/00/0000' to the DLQ.

```python
from datetime import datetime, timezone

def parse_ts(v):
    s = (v or '').strip()
    if s.isdigit() and len(s) == 10:
        return datetime.fromtimestamp(int(s), timezone.utc)
    for f in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%m/%d/%Y %I:%M %p', '%Y-%m-%d', '%d-%b-%Y %H:%M', '%m/%d/%Y', '%d-%m-%Y %H:%M:%S'):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None
```

## 5. `resolved_at`: Placeholders and mixed date formats (warning, action: null_out)

- **Evidence:** '???' 16, text shapes 'aaaaa' 4 and 'aaaaaaa' 2. The same 8 date formats as created_at. Blank 4939 (48.04%).
- **Why it matters:** A wrong resolution timestamp corrupts resolution time and SLA breach metrics.
- **Rule:** Use the same parser as created_at. Null out '???' and any text value. Blank is allowed.

```python
def clean_resolved(v):
    return parse_ts(v) if (v or '').strip() else None
```

## 6. `cross-column`: Resolution timestamp inconsistent with status (warning, action: flag)

- **Evidence:** Closed 1678 + Resolved 1614 = 3292 terminal tickets, but 5341 rows have a resolved_at value (100% - 48.04% = 51.96% of 10280). Non-terminal statuses (Open, In Progress, etc.) therefore likely carry resolved dates.
- **Why it matters:** Contradictory status and resolution dates make backlog counts and resolution time conflict with each other.
- **Rule:** Flag rows where resolved_at is set while status is not Closed/Resolved. Also flag rows where resolved_at is earlier than created_at.

```sql
SELECT *, CASE WHEN COALESCE(resolved_at, '') <> '' AND status NOT IN ('Closed', 'Resolved') THEN 1 ELSE 0 END AS status_resolution_mismatch FROM bronze_tickets
```

## 7. `priority`: Blank and garbage priority values (warning, action: null_out)

- **Evidence:** Blank 1080 (10.51%), 'urgent!!!' 10, 'ASAP' 8, '???' 6, 'null' 4.
- **Why it matters:** Priority drives SLA targets, so tickets without a usable priority get no SLA and fall out of SLA reporting.
- **Rule:** Null out '???', 'null' and blanks. Flag 'urgent!!!' and 'ASAP' for the mapping agent.

```sql
SELECT CASE WHEN TRIM(priority) IN ('', '???', 'null') THEN NULL ELSE priority END AS priority FROM bronze_tickets
```

## 8. `status`: Placeholder status values (warning, action: null_out)

- **Evidence:** 'NULL' 11, '' 10, 'unknown' 6, '???' 3.
- **Why it matters:** Backlog counts depend on status, so these rows cannot be classified as open or closed.
- **Rule:** Status must be in the 6 valid values. Null out and flag everything else.

```sql
SELECT CASE WHEN status IN ('Open', 'In Progress', 'Pending Vendor', 'Escalated', 'Closed', 'Resolved') THEN status END AS status FROM bronze_tickets
```

## 9. `building`: Placeholder and test buildings (info, action: null_out)

- **Evidence:** '???' 10, 'unknown' 9, '' 6, 'test building' 5.
- **Why it matters:** Test rows inflate per-site cost and backlog figures.
- **Rule:** Send 'test building' rows to the DLQ. Null out '???', 'unknown' and blanks.

```sql
SELECT CASE WHEN building IN ('???', 'unknown', '') THEN NULL ELSE building END AS building, building = 'test building' AS is_test FROM bronze_tickets
```

## 10. `submitted_by`: Test/system accounts (info, action: flag)

- **Evidence:** 'test' 11, 'admin' 7, 'system' 5, blank 7.
- **Why it matters:** Synthetic tickets from these accounts pollute volume and backlog metrics.
- **Rule:** Flag rows submitted by test, admin or system.

```sql
SELECT *, LOWER(submitted_by) IN ('test', 'admin', 'system') AS is_test_submitter FROM bronze_tickets
```

## 11. `resolution_notes`: Low-information notes (info, action: flag)

- **Evidence:** 'fixed' 821, 'done' 755, blank 1575.
- **Why it matters:** Root-cause and recurrence analysis is not possible from these notes.
- **Rule:** Flag notes that are 'fixed', 'done' or blank on Closed/Resolved tickets.

```sql
SELECT *, LOWER(TRIM(COALESCE(resolution_notes, ''))) IN ('', 'fixed', 'done') AS weak_notes FROM bronze_tickets
```

## Applied expectations

- `ExpectColumnValuesToMatchRegex` {"column": "ticket_id", "regex": "^TKT-[0-9]{4,5}$"}: The primary key must be well-formed.
- `ExpectColumnValuesToBeUnique` {"column": "ticket_id"}: Duplicate keys break joins and counts.
- `ExpectColumnValuesToBeInSet` {"column": "sla_hours", "value_set": ["4", "8", "24", "48", "72"], "mostly": 0.75}: 999, 0 and -1 are sentinel values, not real SLAs.
- `ExpectColumnValuesToNotBeInSet` {"column": "cost", "value_set": ["-1", "-999", "N/A", "TBD", "error", "NULL"]}: Placeholder costs distort spend totals.
- `ExpectColumnValuesToBeInSet` {"column": "status", "value_set": ["Open", "In Progress", "Pending Vendor", "Escalated", "Closed", "Resolved"]}: Backlog reporting depends on a valid status.
- `ExpectColumnValuesToNotBeNull` {"column": "created_at"}: SLA clocks start at ticket creation.
- `ExpectColumnValuesToNotBeInSet` {"column": "building", "value_set": ["???", "unknown", "test building", ""]}: Placeholder and test buildings must not reach reporting.
- `ExpectTableRowCountToBeBetween` {"min_value": 9000, "max_value": 12000}: Detects truncated or duplicated loads.
