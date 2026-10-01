"""Local dedup state: which Gmail message IDs have already been processed."""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import date

from . import config
from . import notifier

# Google's OAuth "Testing" publish status expires refresh tokens after ~7 days
# regardless of test-user status; publishing to production requires a paid CASA
# security assessment for gmail.modify (a RESTRICTED scope), which isn't realistic for
# a personal single-user tool -- so this expiry is a fact of life, not a bug to fix.
# Warn proactively a day or two ahead so re-auth (scripts/reauth.py) is a scheduled
# 30-second task rather than a mid-run RefreshError crash discovered by surprise.
_TOKEN_WARNING_AGE_DAYS = 5


def warn_if_tokens_aging() -> None:
    """Checks both OAuth token files' mtimes (proxy for when they were last (re)issued
    -- Google's token JSON doesn't itself record the refresh token's issue date) and
    fires one toast notification if either is old enough that the ~7-day expiry could
    hit before the next scheduled run."""
    stale = []
    for label, path in (("Gmail", config.TOKEN_GMAIL_PATH), ("Sheets", config.TOKEN_SHEETS_PATH)):
        if os.path.exists(path):
            age_days = (time.time() - os.path.getmtime(path)) / 86400
            if age_days >= _TOKEN_WARNING_AGE_DAYS:
                stale.append(f"{label} ({age_days:.0f}d old)")
    if stale:
        notifier.notify_needs_review(
            f"Google auth aging: {', '.join(stale)} -- run reauth.py soon before it "
            f"expires (~7 days) and a scheduled run silently fails."
        )


def load_processed_ids() -> set[str]:
    if os.path.exists(config.PROCESSED_IDS_PATH):
        with open(config.PROCESSED_IDS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids: set[str]) -> None:
    with open(config.PROCESSED_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def load_grayed_job_ids() -> set[str]:
    if os.path.exists(config.GRAYED_JOB_IDS_PATH):
        with open(config.GRAYED_JOB_IDS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_grayed_job_ids(job_ids: set[str]) -> None:
    with open(config.GRAYED_JOB_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(job_ids), f, indent=2)


def load_pending_verification() -> dict[str, int]:
    """job_id -> attempt count, for stage 3's give-up-and-drop retry tracking."""
    if os.path.exists(config.PENDING_VERIFICATION_PATH):
        with open(config.PENDING_VERIFICATION_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_pending_verification(pending: dict[str, int]) -> None:
    with open(config.PENDING_VERIFICATION_PATH, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2)


def load_seen_career_postings() -> dict[str, list[str]]:
    if os.path.exists(config.SEEN_CAREER_POSTINGS_PATH):
        with open(config.SEEN_CAREER_POSTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen_career_postings(seen: dict[str, list[str]]) -> None:
    with open(config.SEEN_CAREER_POSTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Neutralizes CSV/spreadsheet-formula injection: company/title/summary here come
    straight from email content an attacker fully controls (e.g. a company name of
    '=cmd|"/c calc"!A1'), and these files are meant to be opened in Excel/Sheets for
    manual review. A cell whose text starts with =, +, -, @, tab or CR is executed as a
    formula by Excel/Sheets/LibreOffice on open; prefixing it with a leading apostrophe
    forces it to be read back as plain text instead, exactly as Excel's own CSV import
    guards against this same class of attack."""
    text = str(value) if value is not None else ""
    return f"'{text}" if text.startswith(_FORMULA_LEAD_CHARS) else text


_SKIPPED_FIELDNAMES = ["date", "company", "title", "reason", "score", "url", "summary"]


def log_skipped_candidate(company: str, title: str, reason: str, score="", url: str = "", summary: str = "") -> None:
    """Recoverable record of a candidate that was scored/evaluated but never written
    to the sheet (low fit, hard-requirement cap). Daily log files rotate and are easy
    to lose track of -- a wrong LLM judgment on a genuinely good role would otherwise
    be unrecoverable once that log's date scrolls out of memory. Append-only, never
    read by any pipeline logic -- purely a human-inspectable audit trail."""
    file_exists = os.path.exists(config.SKIPPED_CANDIDATES_PATH)
    with open(config.SKIPPED_CANDIDATES_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_SKIPPED_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "date": date.today().isoformat(), "company": _csv_safe(company), "title": _csv_safe(title),
            "reason": reason, "score": score, "url": _csv_safe(url), "summary": _csv_safe(summary),
        })


_DROPPED_FIELDNAMES = ["date", "company", "title", "url", "reason", "attempts"]


_dropped_this_process: list[tuple[str, str]] = []


def log_dropped_verification(company: str, title: str, url: str, reason: str, attempts: int) -> None:
    """Recoverable record of an opportunity that verify.py gave up on after
    config.MAX_RESOLUTION_ATTEMPTS and dropped WITHOUT writing any row (see verify.py's
    module docstring) -- e.g. a real, live posting whose link a site like Indeed blocks
    fetching from (HTTP 403 on every attempt). Real incident: a legitimate JLL posting
    was dropped this way and only noticed because the user happened to be watching the
    terminal at the moment it logged -- this file is what lets a drop be reviewed later
    instead of only being visible in that instant. Append-only, never read by pipeline
    logic. Also tracked in-memory (see get_and_clear_dropped_this_run) so main.py can
    fire ONE consolidated end-of-run notification instead of one per drop."""
    _dropped_this_process.append((company, title))
    file_exists = os.path.exists(config.DROPPED_VERIFICATION_PATH)
    with open(config.DROPPED_VERIFICATION_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_DROPPED_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "date": date.today().isoformat(), "company": _csv_safe(company), "title": _csv_safe(title),
            "url": _csv_safe(url), "reason": reason, "attempts": attempts,
        })


def get_and_clear_dropped_this_run() -> list[tuple[str, str]]:
    """(company, title) for every candidate dropped so far this process -- read once
    at the end of a run, then cleared (so a long-lived process, e.g. tests reusing the
    module, never double-counts)."""
    dropped = list(_dropped_this_process)
    _dropped_this_process.clear()
    return dropped


def log_skipped_detail(record: dict) -> None:
    """Append one JSON line with everything needed to re-score or audit a skipped
    candidate later: the exact text that was scored, the extracted requirements with CV
    coverage, the score breakdown and the human-readable reasoning. Never read by pipeline
    logic (see log_skipped_candidate) -- purely an audit/assessment trail."""
    record = {"date": date.today().isoformat(), **record}
    with open(config.SKIPPED_DETAIL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
