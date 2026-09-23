"""One-off deep audit of every active (non-grayed: not closed, not nr) row --
heavier than validate_sheet.py (which is pure/no network), so kept as a separate,
manually-run script rather than folded into the every-run pipeline.

For each active row, checks:
  - identity complete (company/title present, title isn't a generic listing label)
  - url and description both present
  - the stored company checks out against the stored description
    (classifier.confirm_company_name -- one LLM call per row with a description)
  - fit_score is present and a sane 0-100 number (NOT checked: score vs.
    FIT_SCORE_THRESHOLD -- see the comment at that check for why)
  - for rows not yet applied to ONLY (liveness is irrelevant once you've already
    applied): the link is re-fetched and still accepting applications

Two checks were tried and removed after the first real run produced false
positives: a "does the description literally restate the title phrase" grounding
check (job_page_fetcher.extract_snippet_near is designed for a career LISTING page
with many postings, not a single already-scoped job description -- real postings
routinely paraphrase their own title in the body text, e.g. "Software Engineer"
for a stored title of "Senior AI and LLM Solutions Software Engineer"), and the
score-vs-threshold check described above.

Report-only -- never edits the sheet, same philosophy as validate_sheet.py. Findings
go to the log and one summary notification.

Usage:
    python audit_active_positions.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import classifier
from mail_agent import config
from mail_agent import guardrails
from mail_agent import job_page_fetcher
from mail_agent import notifier
from mail_agent import sheets_client

logger = logging.getLogger(__name__)

_LINKEDIN_MARKERS = ("linkedin.com/jobs", "linkedin.com/comm/jobs")
_PRE_APPLICATION_STATUSES = {"", "not applied yet"}


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"audit_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def _check_liveness(url: str) -> str:
    """Returns a problem string, or "" if the link looks fine."""
    try:
        if any(marker in url.lower() for marker in _LINKEDIN_MARKERS):
            posting = job_page_fetcher.fetch_linkedin_posting(url)
        else:
            posting = job_page_fetcher.fetch_generic_posting(url)
    except Exception:
        logger.exception("Liveness fetch errored for url=%s", url)
        return "liveness check errored (see log)"
    if posting.closed:
        return "link shows the posting is no longer accepting applications"
    if not posting.description:
        return "link fetch returned no content (dead/blocked/JS-rendered)"
    return ""


def _check_row(row: dict) -> list[str]:
    problems: list[str] = []
    company, title = (row.get("company") or "").strip(), (row.get("title") or "").strip()
    url, description = (row.get("url") or "").strip(), (row.get("description") or "").strip()
    fit_score = row.get("fit_score", "")
    status = (row.get("status") or "").strip().lower()

    missing = guardrails.missing_identity_fields(row)
    if missing:
        problems.append(f"missing {'/'.join(missing)}")
        return problems  # nothing else meaningful to check without identity

    if guardrails.looks_like_generic_listing_title(title):
        problems.append("title looks like a generic listing label, not a specific position")

    if not url:
        problems.append("no url")
    if not description:
        problems.append("no description")

    if description and company:
        try:
            confirmed = classifier.confirm_company_name(description, company)
        except Exception:
            logger.exception("Company confirmation failed for '%s @ %s'", title, company)
        else:
            confirmed = (confirmed or "").strip()
            if confirmed and confirmed != company:
                problems.append(f"company may be wrong -- description suggests '{confirmed}', sheet says '{company}'")

    if fit_score in ("", None):
        problems.append("no fit_score")
    else:
        try:
            score_val = int(float(fit_score))
        except (TypeError, ValueError):
            problems.append(f"fit_score {fit_score!r} isn't a valid number")
        else:
            if not (0 <= score_val <= 100):
                problems.append(f"fit_score {score_val} out of 0-100 range")
            # NOT checked: score < FIT_SCORE_THRESHOLD. Real false positives found
            # the first time this ran: a row can legitimately carry a sub-threshold
            # score when backfill_career_links.py scores it *after* a real
            # application/reply already happened (that path was never gated --
            # the application is a real historical event regardless of fit), or
            # when HARD_REQUIREMENT_SCORE_CAP capped it by design. This script has
            # no way to tell "gated at insertion" apart from either of those from a
            # flat sheet row, so it doesn't guess.

    # Liveness only matters if you haven't already applied -- once applied, whether
    # the original posting is still accepting new applicants is irrelevant.
    if status in _PRE_APPLICATION_STATUSES and url:
        liveness_problem = _check_liveness(url)
        if liveness_problem:
            problems.append(liveness_problem)

    return problems


def run() -> dict:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set.")
        return {}

    sheets = sheets_client.get_sheets_service()
    rows = sheets_client.fetch_all_rows(sheets)
    active = [r for r in rows if not sheets_client.is_row_closed(r) and not sheets_client.is_row_not_relevant(r)]
    logger.info("[AUDIT] Auditing %d active row(s) (skipping closed/nr).", len(active))

    flagged: dict[int, list[str]] = {}
    for row in active:
        row_number = row["_row"]
        title, company = row.get("title", ""), row.get("company", "")
        try:
            problems = _check_row(row)
        except Exception:
            logger.exception("[AUDIT] row %s '%s @ %s' -> check itself errored, skipping", row_number, title, company)
            continue
        if problems:
            flagged[row_number] = problems
            logger.warning("[AUDIT] row %s '%s @ %s' (status=%r): %s",
                            row_number, title, company, row.get("status", ""), "; ".join(problems))
        else:
            logger.info("[AUDIT] row %s '%s @ %s' -> OK", row_number, title, company)

    logger.info("[AUDIT SUMMARY] %d/%d active row(s) flagged.", len(flagged), len(active))
    if flagged:
        notifier.notify_needs_review(
            f"Sheet audit: {len(flagged)}/{len(active)} active rows flagged -- see audit_*.log for details."
        )
    return flagged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deep audit of every active sheet row")
    parser.parse_args()

    setup_logging()
    run()
