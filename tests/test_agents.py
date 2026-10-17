"""Guardrails that let agent output reach silver without review, and how silver uses it. No LLM or database needed."""
import json

from agents.claude_client import extract_json
from agents.semantic_agent import merge
from src.silver import rules
from src.silver.clean_silver import load_mapping_file

TAXONOMY = ["Electrical", "HVAC", "Other"]
MAPPING = {"elec": "Electrical", "hvac": "HVAC"}
SWAPPED = {"breaker keeps tripping in server room": "Electrical"}


def test_merge_drops_invented_keys_and_off_taxonomy_labels():
    answer = {"mapping": {"elec": "Electrical", "a/c": "Air Con", "made up": "HVAC"},
              "swapped": {"breaker keeps tripping in server room": "Electrical"}, "unmapped": ["???"]}
    mapping, swapped, unmapped, stats = merge([answer], ["elec", "a/c", "breaker keeps tripping in server room", "???"], TAXONOMY)
    assert mapping == {"elec": "Electrical"}
    assert swapped == SWAPPED and unmapped == ["???"]
    assert stats["hallucinated"] == ["made up"]
    assert stats["off_taxonomy"] == ["a/c -> Air Con"]
    assert stats["missing"] == ["a/c"] and stats["coverage_pct"] == 75.0


def test_agent_detected_swap_is_applied_and_sourced():
    raw = "Breaker keeps tripping in server room"
    assert rules.normalize_category(raw, "Elec", {}, SWAPPED) == ("Electrical", raw, True, "category_swap_corrected")
    assert rules.category_source(raw, "Electrical", True, SWAPPED, "agent") == "agent_swap"


def test_rule_swap_still_works_without_agent_swaps():
    category, description, swapped, _ = rules.normalize_category("need more outlets in conf room 387", "Elec", MAPPING)
    assert (category, swapped) == ("Electrical", True)
    assert rules.category_source("need more outlets in conf room 387", category, True, {}, "agent") == "rule_swap"


def test_category_source_for_plain_mapping_and_no_category():
    assert rules.category_source("hvac", "HVAC", False, SWAPPED, "agent") == "agent"
    assert rules.category_source("???", None, False, SWAPPED, "agent") is None


def test_mapping_file_formats(tmp_path):
    flat = tmp_path / "baseline.json"
    flat.write_text(json.dumps({"_comment": "x", "elec": "Electrical"}))
    agent = tmp_path / "agent.json"
    agent.write_text(json.dumps({"_comment": "x", "mapping": MAPPING, "swapped": SWAPPED, "unmapped": ["???"]}))
    assert load_mapping_file(flat) == {"mapping": {"elec": "Electrical"}, "swapped": {}, "unmapped": []}
    assert load_mapping_file(agent) == {"mapping": MAPPING, "swapped": SWAPPED, "unmapped": ["???"]}


def test_claude_json_tolerates_a_code_fence():
    assert extract_json('```json\n{"mapping": {}}\n```') == {"mapping": {}}
    assert extract_json(' {"a": 1} ') == {"a": 1}
