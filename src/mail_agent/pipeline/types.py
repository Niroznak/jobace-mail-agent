"""The typed contracts between pipeline stages. Every stage takes one of these in
and produces one of these out (or None, meaning "this item doesn't continue past
this stage") -- so a failure is always attributable to exactly one stage, and each
stage can be unit-tested against its own input/output shape alone.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candidate:
    """One thing extracted from one email by stage 2 (extract): either a new-
    opportunity candidate (kind="opportunity", goes through stages 3-4-5) or a
    reply/status-update candidate (kind="reply", skips straight to stage 5 --
    there's no position to verify or score, it's about one already tracked, or a
    brand new row created directly from the reply itself).

    `title` is the raw LLM/digest-extracted title, used as-is for matching against
    tracked rows in stage 5 -- deliberately NOT subject-defaulted here, since a
    blank title during *matching* correctly means "no title to conflict with,
    single-row fallback is safe" (see guardrails.resolve_reply_target_row), which
    is a different question from what title a brand new row should get if none of
    the tracked rows match. `subject` is carried separately so stage 5 can apply
    that fallback only at the point it actually creates a new row.
    """
    mail_id: str
    kind: str  # "opportunity" | "reply"
    source: str  # "linkedin_digest" | "generic_digest" | "single_email"
    company: str
    title: str
    subject: str = ""
    location: str = ""
    url: str = ""
    snippet: str = ""
    position_id: str = ""
    status_signal: str = ""  # reply only: applied/interview/offer/rejected
    contact_name: str = ""
    notes: str = ""
    date_utc: str = ""
    # reply only: whether stage 5 may create a brand-new row when no tracked row
    # matches. False for a digest-derived reply candidate (mirrors the original
    # behavior: a digest-shaped "status update" with no resolvable target is just
    # skipped, never promoted to a new row -- only a single-email application_reply
    # ever creates one).
    allow_create_if_unmatched: bool = False


@dataclass
class VerifiedPosition:
    """Stage 3's output: a candidate whose position has been confirmed real, live,
    and grounded -- a real fetched description exists. Never fabricated; if no real
    description could be found, stage 3 returns None instead (see verify.py)."""
    candidate: Candidate
    company: str  # possibly corrected via classifier.confirm_company_name
    title: str
    url: str
    description: str
    job_id: str
    requisition_id: str = ""


@dataclass
class ScoredItem:
    """Stage 4's output: a verified position that scored at or above
    config.FIT_SCORE_THRESHOLD. Below threshold, stage 4 returns None instead
    (logged to skipped_candidates.csv, same as before)."""
    verified: VerifiedPosition
    score: int
    summary: str


@dataclass
class ReconcileResult:
    """Stage 5's output: what actually happened when this item met the sheet."""
    action: str  # "inserted" | "updated" | "duplicate" | "ambiguous" | "dropped"
    row_number: int | None
    detail: str = ""
