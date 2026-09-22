"""Data-trust guardrails -- the single place that defines what counts as complete
enough to write, or sane enough to believe, in this pipeline. Every write path
(position_sheet.append_position, main.py's status-update handling) must pass
through one of these checks before it's allowed to touch the sheet. Three
checkpoints live here:

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
3. Write-target resolution (resolve_reply_target_row): before a status update ever
   touches a row, verifies there is exactly one row it could honestly be about --
   never guesses between multiple candidates or across a stated, conflicting title,
   both confirmed real failure modes (see the function's own docstring).
"""
from __future__ import annotations

from . import sheets_client

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


def resolve_reply_target_row(sheet_rows: list[dict], company: str, title: str) -> tuple[dict | None, list[dict], str]:
    """Finds which tracked row an application-reply/status email is about. Returns
    (row, ambiguous_candidates, match_tier). Exactly one row for the company is
    unambiguous and returned when the reply itself carries no usable title (the
    common case: a terse ack, one tracked role per company -- nothing else it could
    be about) -- UNLESS the incoming email states a specific title that actively
    conflicts with that one row's own specific title, which is strong evidence of a
    second, distinct, never-before-seen position at that company, not "the LLM just
    phrased it differently" (real case: two same-day Mercor rejections, "Excel
    Expert - Finance" and "Excel Expert - General", silently merged into one row
    before this check existed -- both had real, distinct role_titles, so this wasn't
    a "missing title" case at all). With multiple rows, an exact title match
    disambiguates; failing that, returns (None, candidates, "") rather than guessing
    -- silently picking "first row for this company" is exactly what wrote an
    "applied" status to the wrong Mobileye row (and, combined with a company-name
    mismatch, created a stray duplicate for Micron/"Micron Technology") before that
    guard existed."""
    title_match = sheets_client.find_row_by_company_and_title(sheet_rows, company, title)
    if title_match:
        return title_match, [], "title_match"
    candidates = sheets_client.find_rows_by_company(sheet_rows, company)
    if len(candidates) <= 1:
        single = candidates[0] if candidates else None
        if single and title.strip() and single.get("title", "").strip():
            if sheets_client.normalize_title(title) != sheets_client.normalize_title(single["title"]):
                return None, [single], "title_conflict"
        return single, [], "single_company_row"
    return None, candidates, ""
