"""Rule tests use real rows from data/raw_tickets.csv, not invented inputs."""
from datetime import datetime
from decimal import Decimal

import pytest

from src.bronze.load_bronze import row_hash
from src.silver import dlq, rules

MAPPING = {"elec": "Electrical", "other": "Other", "pest": "Pest Control", "hvac": "HVAC", "a/c": "HVAC"}


@pytest.mark.parametrize("raw, expected", [
    ("2024-10-24 14:32:10", datetime(2024, 10, 24, 14, 32, 10)),
    ("2024-07-25T18:27:44", datetime(2024, 7, 25, 18, 27, 44)),
    ("28-Feb-2024 17:03", datetime(2024, 2, 28, 17, 3)),
    ("02/14/2025 08:16 AM", datetime(2025, 2, 14, 8, 16)),
    ("03-20-2024 20:39:48", datetime(2024, 3, 20, 20, 39, 48)),
    ("04/09/2025", datetime(2025, 4, 9)),
    ("2024-04-15", datetime(2024, 4, 15)),
    ("1726547768", datetime(2024, 9, 17, 4, 36, 8)),
])
def test_parse_timestamp_all_observed_formats(raw, expected):
    assert rules.parse_timestamp(raw) == (expected, False)


@pytest.mark.parametrize("raw", ["not a date", "TBD", "asap", "00/00/0000", "???"])
def test_parse_timestamp_garbage_is_flagged_invalid(raw):
    value, invalid = rules.parse_timestamp(raw)
    assert value is None and invalid == (raw.strip().lower() not in rules.BLANK)


def test_parse_timestamp_blank_is_missing_not_invalid():
    assert rules.parse_timestamp("") == (None, False)


@pytest.mark.parametrize("raw, amount, flag", [
    ("$3065.86", Decimal("3065.86"), None),
    ("4569.86", Decimal("4569.86"), None),
    ("5877", Decimal("5877.00"), None),
    ("0", Decimal("0.00"), None),
    ("-1", None, "cost_sentinel"),
    ("-999", None, "cost_sentinel"),
    ("error", None, "cost_unparseable"),
    ("TBD", None, None),
    ("N/A", None, None),
    ("", None, None),
])
def test_parse_cost(raw, amount, flag):
    assert rules.parse_cost(raw) == (amount, flag)


@pytest.mark.parametrize("raw, hours, flag", [
    ("48", 48, None), ("4", 4, None), ("999", None, "sla_out_of_range"), ("0", None, "sla_out_of_range"),
    ("-1", None, "sla_out_of_range"), ("N/A", None, None), ("", None, None),
    ("48h", 48, None), ("8 hrs", 8, None), ("0h", None, "sla_out_of_range"),
])
def test_parse_sla_hours(raw, hours, flag):
    assert rules.parse_sla_hours(raw) == (hours, flag)


@pytest.mark.parametrize("raw, expected", [
    ("lo", "LOW"), ("LOW", "LOW"), ("med", "MEDIUM"), ("Normal", "MEDIUM"), ("hi", "HIGH"),
    ("crit", "CRITICAL"), ("CRITICAL", "CRITICAL"),
])
def test_normalize_priority_known_variants(raw, expected):
    assert rules.normalize_priority(raw) == (expected, None)


def test_normalize_priority_free_text_and_junk():
    assert rules.normalize_priority("urgent!!!") == ("CRITICAL", "priority_inferred")
    assert rules.normalize_priority("???") == (None, "priority_placeholder")
    assert rules.normalize_priority("") == (None, None)


def test_normalize_status():
    assert rules.normalize_status("pending vendor") == ("Pending Vendor", None)
    assert rules.normalize_status("unknown") == (None, "status_placeholder")
    assert rules.normalize_status("NULL") == (None, None)


def test_category_variants_collapse_to_canonical():
    assert rules.normalize_category("  A/C ", "cold", MAPPING) == ("HVAC", "cold", False, None)


def test_swapped_category_and_description_are_restored():
    # TKT-11268 as it appears in the raw file
    category, description, swapped, flag = rules.normalize_category("need more outlets in conf room 387", "Elec", MAPPING)
    assert (category, description, swapped, flag) == (
        "Electrical", "need more outlets in conf room 387", True, "category_swap_corrected")


def test_unknown_category_is_flagged_not_guessed():
    assert rules.normalize_category("zzz new thing", "Breaker tripped", MAPPING)[3] == "category_unmapped"
    assert rules.normalize_category("test", "", MAPPING)[3] == "category_placeholder"


def test_clean_row_flags_resolution_before_creation():
    raw = {"created_at": "2024-06-16T04:31:56", "resolved_at": "2024-03-14 00:39:27", "category": "A/C",
           "priority": "Medium", "status": "Open", "building": "Tower B", "description": "cold",
           "submitted_by": "pat anderson", "assigned_to": "PestPro Services", "resolution_notes": "",
           "cost": "4569.86", "sla_hours": "4"}
    cleaned, flags = rules.clean_row(raw, MAPPING)
    assert "resolved_before_created" in flags and "status_resolved_at_mismatch" in flags
    assert cleaned["submitted_by"] == "Pat Anderson" and cleaned["cost"] == Decimal("4569.86")


@pytest.mark.parametrize("ticket_id", ["15", "N/A", "", "NULL"])
def test_junk_ticket_ids_go_to_dlq(ticket_id):
    assert dlq.invalid_id_reason(ticket_id) == "invalid_ticket_id"


def test_valid_ticket_id_passes():
    assert dlq.invalid_id_reason("TKT-3960") is None


def test_row_hash_is_stable_and_content_sensitive():
    assert row_hash(["a", "b"], None) == row_hash(["a", "b"], None)
    assert row_hash(["a", "b"], None) != row_hash(["a", "c"], None)
    assert row_hash(["a", "b"], None) != row_hash(["a", "b"], {"new_col": "x"})
