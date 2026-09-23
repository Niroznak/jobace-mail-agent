"""Reviews every tracked, non-closed row's link and grays out ones that are no longer
open -- keeps history (never deletes), and keeps the active set clean for main.py's
dedup. Intended to run before main.py each cycle (see run_mail_agent.bat).

Usage:
    python review_closed_positions.py             # marks closed rows, grays them out
    python review_closed_positions.py --dry-run    # logs intended actions only
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import company_directory
from mail_agent import config
from mail_agent import job_page_fetcher
from mail_agent import sheets_client
from mail_agent import state

logger = logging.getLogger(__name__)

_LINKEDIN_MARKERS = ("linkedin.com/jobs", "linkedin.com/comm/jobs")
_CLOSED_ROW_COLOR = (0.7, 0.7, 0.7)
_PACE_SECONDS = 2


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"review_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def _is_career_directory_link(url: str, directory_rows: list[dict]) -> bool:
    return any(row.get("Link", "").strip() == url for row in directory_rows)


def _is_stale_not_applied(row: dict, today: datetime) -> bool:
    """True if this row has sat at a pre-application status for
    config.STALE_NOT_APPLIED_DAYS or longer -- a priority judgment call (see
    config.py), independent of whether the link itself still resolves."""
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


def _check_row(row: dict, directory_rows: list[dict]) -> bool | None:
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


def run(dry_run: bool = False) -> None:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set. Set the JOBACE_SHEET_ID env var or edit config.py.")
        return

    sheets = sheets_client.get_sheets_service()
    rows = sheets_client.fetch_all_rows(sheets)
    directory_rows = company_directory.load()

    grayed_job_ids = state.load_grayed_job_ids()
    nr_rows = [
        r for r in rows
        if sheets_client.is_row_not_relevant(r) and r.get("job_id", "") not in grayed_job_ids
    ]
    if nr_rows:
        logger.info("Graying %d newly-marked 'nr' row(s)%s.", len(nr_rows), " (dry-run)" if dry_run else "")
    for row in nr_rows:
        logger.info("[NR] row %s '%s @ %s' -> grayed.", row["_row"], row.get("title", ""), row.get("company", ""))
        if not dry_run:
            sheets_client.set_row_text_color(sheets, row["_row"], _CLOSED_ROW_COLOR)
            job_id = row.get("job_id", "")
            if job_id:
                grayed_job_ids.add(job_id)
    if not dry_run and nr_rows:
        state.save_grayed_job_ids(grayed_job_ids)

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
            notes = sheets_client.append_status_history(
                row.get("notes", ""), "nr", today.strftime("%Y-%m-%d"),
            )
            sheets_client.update_row_fields(sheets, row_number, {
                "status": "nr",
                "notes": f"[AUTO] No application after {config.STALE_NOT_APPLIED_DAYS}+ days. {notes}".strip(),
            })
            sheets_client.set_row_text_color(sheets, row_number, _CLOSED_ROW_COLOR)
            job_id = row.get("job_id", "")
            if job_id:
                grayed_job_ids.add(job_id)
    if not dry_run and stale_rows:
        state.save_grayed_job_ids(grayed_job_ids)
    stale_row_numbers = {r["_row"] for r in stale_rows}

    candidates = [
        r for r in rows
        if sheets_client.is_eligible_for_closure(r) and r.get("url", "").strip()
        and r["_row"] not in stale_row_numbers
    ]
    logger.info("Reviewing %d open row(s) with a link%s.", len(candidates), " (dry-run)" if dry_run else "")

    closed_count = 0
    unknown_count = 0
    for row in candidates:
        time.sleep(_PACE_SECONDS)
        title, company, row_number = row.get("title", ""), row.get("company", ""), row["_row"]
        try:
            closed = _check_row(row, directory_rows)
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
                today = datetime.now().strftime("%Y-%m-%d")
                notes = sheets_client.append_status_history(row.get("notes", ""), "closed", today)
                sheets_client.update_row_fields(sheets, row_number, {"status": "closed", "notes": notes})
                sheets_client.set_row_text_color(sheets, row_number, _CLOSED_ROW_COLOR)
        else:
            logger.debug("[ACTIVE] row %s '%s @ %s' unchanged.", row_number, title, company)

    logger.info(
        "Review done: %d closed, %d inconclusive, %d unchanged.",
        closed_count, unknown_count, len(candidates) - closed_count - unknown_count,
    )

    if not dry_run:
        sheets_client.refresh_basic_filter(sheets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review tracked positions and gray out closed ones")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions without writing")
    args = parser.parse_args()

    setup_logging()
    run(dry_run=args.dry_run)
