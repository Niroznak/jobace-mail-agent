"""Data-trust guardrails -- the single place that defines what counts as complete
enough to write, or sane enough to believe, in this pipeline. Two checkpoints live
here:

1. Identity completeness (a sheet row must name a company and a title to be
   findable/dedup-able later). Used by position_sheet.append_position on write, and
   by validate_sheet.py's post-run audit, so both agree on the same definition of
   "incomplete" instead of drifting apart.
2. LLM output plausibility (classifier.classify_email's triage result). Ollama's
   `format: json` only guarantees syntactically valid JSON, not sane *content* --
   the model has been observed to leak raw chain-of-thought reasoning into a string
   field and invent a status to match its own fabrication (confirmed case: a
   rejection status invented for a role with no reject email at all, ~400 chars of
   leaked reasoning in "notes"). A real one-sentence summary is never that long, so
   length is a cheap, reliable tripwire -- far cheaper than pattern-matching every
   language the leak could appear in.
"""
from __future__ import annotations

REQUIRED_IDENTITY_FIELDS = ("company", "title")


def missing_identity_fields(fields: dict) -> list[str]:
    """Which of the required identity fields are blank/whitespace-only."""
    return [f for f in REQUIRED_IDENTITY_FIELDS if not (fields.get(f) or "").strip()]


def tag_incomplete(fields: dict, missing: list[str]) -> dict:
    """Returns a copy of `fields` with a review tag prepended to notes -- never drops
    the row, just makes the gap visible instead of silently writing it."""
    tag = f"[NEEDS REVIEW: missing {'/'.join(missing)}]"
    return {**fields, "notes": (tag + " " + (fields.get("notes", "") or "")).strip()}


VALID_TRIAGE_STATUSES = {"", "applied", "interview", "offer", "rejected"}
MAX_TRIAGE_NOTES_CHARS = 220


def looks_like_hallucinated_triage(notes: str, status: str) -> bool:
    """True if an LLM triage result looks fabricated rather than a real, considered
    answer: either the status isn't one of the values the prompt actually offers, or
    "notes" (meant to be one short sentence) is implausibly long for that."""
    return len(notes or "") > MAX_TRIAGE_NOTES_CHARS or status not in VALID_TRIAGE_STATUSES
