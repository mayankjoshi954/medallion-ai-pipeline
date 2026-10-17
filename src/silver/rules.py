"""Silver cleaning rules: pure functions, no I/O, so each rule is unit-testable in isolation.

Policy: clean, flag and KEEP. A row is only rejected (see dlq.py) when it is structurally unusable.
A flag is raised only when we altered or discarded a non-blank value; plain blanks become NULL silently.
"""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

BLANK = {"", "null", "n/a", "na", "none", "nan"}
PLACEHOLDER = {"unknown", "???", "tbd", "test", "asdf", "delete me", "test building", "admin", "system"}

PRIORITY_MAP = {
    "low": "LOW", "lo": "LOW",
    "medium": "MEDIUM", "med": "MEDIUM", "normal": "MEDIUM",
    "high": "HIGH", "hi": "HIGH",
    "critical": "CRITICAL", "crit": "CRITICAL",
}
PRIORITY_INFERRED = {"urgent!!!": "CRITICAL", "asap": "CRITICAL"}

STATUS_MAP = {
    "open": "Open", "in progress": "In Progress", "pending vendor": "Pending Vendor",
    "escalated": "Escalated", "resolved": "Resolved", "closed": "Closed",
}
OPEN_STATES = {"Open", "In Progress", "Pending Vendor", "Escalated"}
DONE_STATES = {"Resolved", "Closed"}

SLA_MIN, SLA_MAX = 1, 720  # observed real tiers are 4/8/24/48/72h; anything outside is a sentinel

# Slash and dash numeric dates are month-first: profiling showed the first field never exceeds 12.
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p", "%d-%b-%Y %H:%M",
    "%m-%d-%Y %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y",
)


def norm_key(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def clean_text(value: str | None) -> tuple[str | None, bool]:
    """Return (value or None, was_placeholder). Blank-like tokens are quietly missing; junk tokens are flagged."""
    key = norm_key(value)
    if key in BLANK:
        return None, False
    if key in PLACEHOLDER:
        return None, True
    return re.sub(r"\s+", " ", value.strip()), False


def parse_timestamp(value: str | None) -> tuple[datetime | None, bool]:
    """Return (naive UTC datetime or None, was_invalid). Epoch seconds are interpreted as UTC."""
    key = norm_key(value)
    if key in BLANK:
        return None, False
    text = value.strip()
    if re.fullmatch(r"\d{10}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).replace(tzinfo=None), False
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if 2000 <= parsed.year <= 2100:
            return parsed, False
    return None, True


def parse_cost(value: str | None) -> tuple[Decimal | None, str | None]:
    """Return (amount or None, flag or None)."""
    key = norm_key(value)
    if key in BLANK or key == "tbd":
        return None, None
    try:
        amount = Decimal(key.lstrip("$").replace(",", ""))
    except InvalidOperation:
        return None, "cost_unparseable"
    if not amount.is_finite():
        return None, "cost_unparseable"
    if amount < 0:
        return None, "cost_sentinel"
    return amount.quantize(Decimal("0.01")), None


def parse_sla_hours(value: str | None) -> tuple[int | None, str | None]:
    key = norm_key(value)
    if key in BLANK:
        return None, None
    key = re.sub(r"\s*(h|hr|hrs|hours?)$", "", key)  # '48h' appeared when the source changed its format
    try:
        hours = int(Decimal(key))
    except (InvalidOperation, OverflowError, ValueError):
        return None, "sla_out_of_range"
    if not SLA_MIN <= hours <= SLA_MAX:
        return None, "sla_out_of_range"
    return hours, None


def normalize_priority(value: str | None) -> tuple[str | None, str | None]:
    key = norm_key(value)
    if key in BLANK:
        return None, None
    if key in PRIORITY_MAP:
        return PRIORITY_MAP[key], None
    if key in PRIORITY_INFERRED:
        return PRIORITY_INFERRED[key], "priority_inferred"
    return None, "priority_placeholder"


def normalize_status(value: str | None) -> tuple[str | None, str | None]:
    key = norm_key(value)
    if key in BLANK:
        return None, None
    if key in STATUS_MAP:
        return STATUS_MAP[key], None
    return None, "status_placeholder"


def normalize_category(raw_category, raw_description, mapping: dict[str, str], swapped: dict[str, str] | None = None):
    """Return (category, description, swapped, flag).

    Some rows have the two columns swapped: free text in category, the real category in description.
    A sentence the semantic agent recognised as swapped (`swapped`) is used first; otherwise we swap back only
    when category is unknown to the mapping AND description is a known category.
    """
    cat_key, desc_key = norm_key(raw_category), norm_key(raw_description)
    if cat_key in mapping:
        return mapping[cat_key], raw_description, False, None
    cat_text, cat_placeholder = clean_text(raw_category)
    if cat_text is not None and swapped and cat_key in swapped:
        return swapped[cat_key], cat_text, True, "category_swap_corrected"
    if cat_text is not None and desc_key in mapping:
        return mapping[desc_key], cat_text, True, "category_swap_corrected"
    if cat_placeholder:
        return None, raw_description, False, "category_placeholder"
    if cat_text is not None:
        return None, raw_description, False, "category_unmapped"
    return None, raw_description, False, None


def category_source(raw_category, category, was_swapped, swapped, mapping_source: str) -> str | None:
    """Where the canonical category came from: '<source>' (mapped), '<source>_swap' (agent), 'rule_swap', or None."""
    if category is None:
        return None
    if not was_swapped:
        return mapping_source
    return f"{mapping_source}_swap" if swapped and norm_key(raw_category) in swapped else "rule_swap"


def clean_row(raw: dict, category_mapping: dict[str, str], swapped: dict[str, str] | None = None,
              mapping_source: str = "baseline") -> tuple[dict, list[str]]:
    """Apply every rule to one bronze row. Returns (silver column values, flags)."""
    flags: list[str] = []

    created_at, bad = parse_timestamp(raw["created_at"])
    if bad:
        flags.append("created_at_invalid")
    resolved_at, bad = parse_timestamp(raw["resolved_at"])
    if bad:
        flags.append("resolved_at_invalid")
    if created_at and resolved_at and resolved_at < created_at:
        flags.append("resolved_before_created")

    category, description, was_swapped, flag = normalize_category(raw["category"], raw["description"],
                                                                  category_mapping, swapped)
    source = category_source(raw["category"], category, was_swapped, swapped, mapping_source)
    if flag:
        flags.append(flag)
    priority, flag = normalize_priority(raw["priority"])
    if flag:
        flags.append(flag)
    status, flag = normalize_status(raw["status"])
    if flag:
        flags.append(flag)
    if (status in OPEN_STATES and resolved_at is not None) or (status in DONE_STATES and resolved_at is None):
        flags.append("status_resolved_at_mismatch")

    building, placeholder = clean_text(raw["building"])
    if placeholder:
        flags.append("building_placeholder")
    submitted_by, placeholder = clean_text(raw["submitted_by"])
    if placeholder:
        flags.append("submitted_by_placeholder")
    cost, flag = parse_cost(raw["cost"])
    if flag:
        flags.append(flag)
    sla_hours, flag = parse_sla_hours(raw["sla_hours"])
    if flag:
        flags.append(flag)

    description, _ = clean_text(description)
    assigned_to, _ = clean_text(raw["assigned_to"])
    notes, _ = clean_text(raw["resolution_notes"])
    return {
        "created_at": created_at,
        "resolved_at": resolved_at,
        "category": category,
        "category_raw": raw["category"] or None,
        "category_swap_corrected": was_swapped,
        "category_source": source,
        "priority": priority,
        "status": status,
        "building": building,
        "description": description,
        "submitted_by": submitted_by.title() if submitted_by else None,
        "assigned_to": assigned_to,
        "resolution_notes": notes,
        "cost": cost,
        "sla_hours": sla_hours,
    }, flags
