"""Daily integrity check over every open (non-closed/non-nr) tracked position:
availability (is the link still live), completeness (identity fields, url/description
present), and duplicates (two active rows resolving to the same dedup key).

Merges what were three separately-scheduled, overlapping scripts:
  - review_closed_positions.py (liveness re-fetch, staleness auto-nr, grayed-nr tracking)
  - validate_sheet.py (identity/content-completeness, report-only)
  - a new duplicate check (active rows sharing a dedup.job_id_for key)
into one report you read in one place, once a day.

Real incident this replaces: the liveness re-fetch (one network call to LinkedIn/Indeed
per open row) used to run on EVERY mail-scan cycle (every ~10 min via run_mail_agent.bat)
-- that volume of requests is what tripped LinkedIn's own bot-detection (HTTP 429/999)
22 times in a single run. This script is intended to run once a day instead (see
run_morning.bat), cutting that request volume by roughly two orders of magnitude.

Usage:
    python check_open_positions.py             # marks closed/stale rows, grays them out
    python check_open_positions.py --dry-run    # logs intended actions only, no writes
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import company_directory
from mail_agent import config
from mail_agent import guardrails
from mail_agent import job_page_fetcher
from mail_agent import notifier
from mail_agent import sheets_client
from mail_agent import state
from mail_agent.pipeline import dedup

logger = logging.getLogger(__name__)

_LINKEDIN_MARKERS = ("linkedin.com/jobs", "linkedin.com/comm/jobs")
_CLOSED_ROW_COLOR = (0.7, 0.7, 0.7)
_PACE_SECONDS = 2


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"check_open_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


# --- shared row classification (same semantics as the scripts this merges) ---

def _is_terminal(row: dict) -> bool:
    """closed/nr rows are done -- already grayed out, nothing in this pipeline ever
    revisits them. Re-flagging a historical gap on one of these every run is pure
    noise: the row was already reviewed once (that's what put it in this state)."""
    return sheets_client.is_row_closed(row) or sheets_client.is_row_not_relevant(row)


def _missing_content(row: dict) -> bool:
    """An active, trackable row with neither a link nor a description is unreviewable
    -- there's nothing to look at to judge fit or find the posting."""
    if _is_terminal(row):
        return False
    return not row.get("url", "").strip() and not row.get("description", "").strip()


def _grayed_key(row: dict) -> str:
    """Stable-enough identity for "already grayed" tracking -- job_id when present,
    else the row number itself. Real bug: rows with a blank job_id (created before it
    was consistently populated, or added/edited by hand) could never be remembered as
    already-handled under job_id alone, so they got re-logged/re-grayed every run,
    forever. Row numbers are stable since this codebase never deletes rows."""
    job_id = (row.get("job_id") or "").strip()
    return job_id if job_id else f"row:{row['_row']}"


def _is_stale_not_applied(row: dict, today: datetime) -> bool:
    """True if this row has sat at a pre-application status for
    config.STALE_NOT_APPLIED_DAYS or longer -- a priority judgment call, independent
    of whether the link itself still resolves."""
    if not sheets_client.is_eligible_for_closure(row):
        return False
    date_saved = (row.get("date_saved") or "").strip()
    if not date_saved:
        return False
    try:
        saved = datetime.strptime(date_saved, "%Y-%m-%d")
    except ValueError:
        return False
    return (today - saved) >= timedelta(days=config.STALE_NOT_APPLIED_DAYS)


def _is_career_directory_link(url: str, directory_rows: list[dict]) -> bool:
    return any(row.get("Link", "").strip() == url for row in directory_rows)


def _check_liveness(row: dict, directory_rows: list[dict]) -> bool | None:
    """Returns True if closed, False if active, None if the fetch was inconclusive
    (never mark closed on an ambiguous fetch failure)."""
    url = row.get("url", "").strip()
    if any(marker in url.lower() for marker in _LINKEDIN_MARKERS):
        posting = job_page_fetcher.fetch_linkedin_posting(url)
        if not posting.description and not posting.closed:
            return None
        return posting.closed

    if _is_career_directory_link(url, directory_rows):
        posting = job_page_fetcher.fetch_generic_posting(url)
        if not posting.description:
            return None
        snippet = job_page_fetcher.extract_snippet_near(posting.description, row.get("title", ""))
        return not bool(snippet)

    posting = job_page_fetcher.fetch_generic_posting(url)
    if not posting.description:
        return None
    return posting.closed


def find_duplicate_groups(rows: list[dict]) -> list[list[dict]]:
    """Active (non-terminal) rows sharing the same company+title dedup key -- the same
    key `verify.py` checks new candidates against, so two rows sharing one mean a
    duplicate slipped through (e.g. the CaliAlfa / "CaliAlfa (Previously Alfabet)"
    incident: a company-name spelling difference hashed to two keys before that was
    fixed in dedup.job_id_for). Reports every group with more than one row."""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if _is_terminal(row):
            continue
        company, title = row.get("company", "").strip(), row.get("title", "").strip()
        if not company or not title:
            continue
        key = dedup.job_id_for(company, title)
        by_key[key].append(row)
    return [group for group in by_key.values() if len(group) > 1]


def run(dry_run: bool = False) -> dict:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set. Set the JOBACE_SHEET_ID env var or edit config.py.")
        return {}

    sheets = sheets_client.get_sheets_service()
    rows = sheets_client.fetch_all_rows(sheets)
    directory_rows = company_directory.load()

    # --- 1. Gray out anything already marked nr but not yet visually grayed ---
    grayed_job_ids = state.load_grayed_job_ids()
    nr_rows = [
        r for r in rows
        if sheets_client.is_row_not_relevant(r) and _grayed_key(r) not in grayed_job_ids
    ]
    if nr_rows:
        logger.info("Graying %d newly-marked 'nr' row(s)%s.", len(nr_rows), " (dry-run)" if dry_run else "")
    for row in nr_rows:
        logger.info("[NR] row %s '%s @ %s' -> grayed.", row["_row"], row.get("title", ""), row.get("company", ""))
        if not dry_run:
            sheets_client.set_row_text_color(sheets, row["_row"], _CLOSED_ROW_COLOR)
            grayed_job_ids.add(_grayed_key(row))
    if not dry_run and nr_rows:
        state.save_grayed_job_ids(grayed_job_ids)

    # --- 2. Auto-deprioritize stale not-applied rows ---
    today = datetime.now()
    stale_rows = [r for r in rows if _is_stale_not_applied(r, today)]
    if stale_rows:
        logger.info("Deprioritizing %d row(s) stale %d+ days with no application%s.",
                     len(stale_rows), config.STALE_NOT_APPLIED_DAYS, " (dry-run)" if dry_run else "")
    for row in stale_rows:
        row_number, title, company = row["_row"], row.get("title", ""), row.get("company", "")
        logger.info("[STALE] row %s '%s @ %s' -> saved %s, no application in %d+ days, marked nr.",
                     row_number, title, company, row.get("date_saved", ""), config.STALE_NOT_APPLIED_DAYS)
        if not dry_run:
            notes = sheets_client.append_status_history(row.get("notes", ""), "nr", today.strftime("%Y-%m-%d"))
            sheets_client.update_row_fields(sheets, row_number, {
                "status": "nr",
                "notes": f"[AUTO] No application after {config.STALE_NOT_APPLIED_DAYS}+ days. {notes}".strip(),
            })
            sheets_client.set_row_text_color(sheets, row_number, _CLOSED_ROW_COLOR)
            grayed_job_ids.add(_grayed_key(row))
    if not dry_run and stale_rows:
        state.save_grayed_job_ids(grayed_job_ids)
    stale_row_numbers = {r["_row"] for r in stale_rows}

    # --- 3. Liveness re-fetch for every remaining open row with a link ---
    liveness_candidates = [
        r for r in rows
        if sheets_client.is_eligible_for_closure(r) and r.get("url", "").strip()
        and r["_row"] not in stale_row_numbers
    ]
    logger.info("Checking liveness of %d open row(s) with a link%s.", len(liveness_candidates), " (dry-run)" if dry_run else "")
    closed_count = unknown_count = 0
    for row in liveness_candidates:
        time.sleep(_PACE_SECONDS)
        title, company, row_number = row.get("title", ""), row.get("company", ""), row["_row"]
        try:
            closed = _check_liveness(row, directory_rows)
        except Exception:
            logger.exception("Failed checking row %s '%s @ %s' -> leaving unchanged", row_number, title, company)
            continue
        if closed is None:
            unknown_count += 1
            logger.debug("[UNKNOWN] row %s '%s @ %s' -> fetch inconclusive, leaving unchanged.", row_number, title, company)
        elif closed:
            closed_count += 1
            logger.info("[CLOSED] row %s '%s @ %s' -> marked + grayed.", row_number, title, company)
            if not dry_run:
                today_str = datetime.now().strftime("%Y-%m-%d")
                notes = sheets_client.append_status_history(row.get("notes", ""), "closed", today_str)
                sheets_client.update_row_fields(sheets, row_number, {"status": "closed", "notes": notes})
                sheets_client.set_row_text_color(sheets, row_number, _CLOSED_ROW_COLOR)
        else:
            logger.debug("[ACTIVE] row %s '%s @ %s' unchanged.", row_number, title, company)

    # --- 4. Completeness: identity + url/description gaps (report-only) ---
    identity_problems, content_problems = [], []
    for row in rows:
        if _is_terminal(row):
            continue
        missing = guardrails.missing_identity_fields(row)
        if missing:
            identity_problems.append((row["_row"], missing))
        elif _missing_content(row):
            attempts = sheets_client.parse_attempt_count(row.get("notes", ""))
            content_problems.append((row["_row"], attempts, row))
    for row_number, missing in identity_problems:
        logger.warning("[VALIDATE] row %s missing %s", row_number, "/".join(missing))
    for row_number, attempts, row in content_problems:
        logger.warning(
            "[VALIDATE] row %s '%s @ %s' has no url or description (status=%r, resolution attempts=%d)",
            row_number, row.get("title", ""), row.get("company", ""), row.get("status", ""), attempts,
        )

    # --- 5. Duplicates: active rows sharing a dedup key ---
    duplicate_groups = find_duplicate_groups(rows)
    for group in duplicate_groups:
        row_numbers = [r["_row"] for r in group]
        logger.warning(
            "[DUPLICATE] rows %s all resolve to the same dedup key ('%s @ %s') -- review manually.",
            row_numbers, group[0].get("title", ""), group[0].get("company", ""),
        )

    logger.info(
        "Liveness: %d closed, %d inconclusive, %d unchanged. Completeness: %d identity gap(s), "
        "%d missing content. Duplicates: %d group(s).",
        closed_count, unknown_count, len(liveness_candidates) - closed_count - unknown_count,
        len(identity_problems), len(content_problems), len(duplicate_groups),
    )

    total_issues = len(identity_problems) + len(content_problems) + len(duplicate_groups)
    if total_issues:
        summary = (
            f"Daily integrity check: {len(identity_problems)} missing company/title, "
            f"{len(content_problems)} missing url/description, {len(duplicate_groups)} duplicate group(s). "
            f"See check_open_*.log for details."
        )
        logger.warning("[RUN SUMMARY] %s", summary)
        notifier.notify_needs_review(summary)

    if not dry_run:
        sheets_client.refresh_basic_filter(sheets)

    return {
        "closed": closed_count,
        "identity_problems": [r for r, _ in identity_problems],
        "content_problems": [r for r, _, _ in content_problems],
        "duplicate_groups": [[r["_row"] for r in g] for g in duplicate_groups],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily integrity check: liveness, completeness, duplicates")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions without writing")
    args = parser.parse_args()

    setup_logging()
    state.warn_if_tokens_aging()  # proactive heads-up before Google's ~7-day Testing expiry hits
    run(dry_run=args.dry_run)
