"""Drift tests on header-only files: there are no values to infer types from, so only column names are compared."""
from src.schema import drift

BASELINE = {"ticket_id": {"dtype": "string", "nullable": False}, "cost": {"dtype": "float64", "nullable": True}}


def test_header_only_file_with_known_columns_has_no_drift(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("ticket_id,cost\n")
    assert not drift.has_drift(drift.diff(BASELINE, drift.infer_snapshot(csv, BASELINE)))


def test_header_only_file_still_reports_added_and_removed_columns(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("ticket_id,contractor_id\n")
    d = drift.diff(BASELINE, drift.infer_snapshot(csv, BASELINE))
    assert set(d["added"]) == {"contractor_id"}
    assert set(d["removed"]) == {"cost"}
    assert not d["dtype_changed"]
