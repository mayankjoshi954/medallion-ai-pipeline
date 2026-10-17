"""Dead-letter policy: what we refuse to promote to silver.

Keep-vs-reject principle: reject only when a row cannot be placed in the model at all. A row with a bad
priority or cost is still a real ticket, so it is cleaned, flagged and kept. A row with no valid ticket_id
or no creation time cannot be keyed or put on a timeline, so it goes to silver.rejected_rows with its
raw payload intact.
"""
import re

TICKET_ID = re.compile(r"TKT-\d+")


def invalid_id_reason(raw_ticket_id: str | None) -> str | None:
    return None if TICKET_ID.fullmatch((raw_ticket_id or "").strip()) else "invalid_ticket_id"


def missing_timeline_reason(cleaned: dict) -> str | None:
    return "missing_created_at" if cleaned["created_at"] is None else None


def reprocess_dlq() -> None:
    """TODO: replay silver.rejected_rows through clean_row after a rule fix, then close out fixed entries."""
    raise NotImplementedError
