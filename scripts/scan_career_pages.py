"""Proactively scans every tracked company's career page (tracked_companies.csv) for
new postings, instead of waiting for LinkedIn/email to surface them. A candidate link
is only ever considered once per company (see state.seen_career_postings) so repeat
runs stay cheap and quiet. Best-effort by nature: many career sites are JS-rendered
and a plain fetch of their listing page returns no links at all (confirmed case:
career.rafael.co.il) -- that's a real coverage gap, not a bug, logged distinctly.

Usage:
    python scan_career_pages.py             # scans, scores, appends new matches
    python scan_career_pages.py --dry-run    # logs intended actions only
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import classifier
from mail_agent import company_directory
from mail_agent import config
from mail_agent import cv_matcher
from mail_agent import job_page_fetcher
import main
from mail_agent import notifier
from mail_agent import position_sheet
from mail_agent import sheets_client
from mail_agent import state

logger = logging.getLogger(__name__)

_PACE_SECONDS = 2
_NUMERIC_ID_RE = re.compile(r"(\d{3,})")


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"scan_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def _position_id_from_url(url: str) -> str:
    match = _NUMERIC_ID_RE.search(url)
    return match.group(1) if match else ""


def run(dry_run: bool = False) -> None:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set. Set the JOBACE_SHEET_ID env var or edit config.py.")
        return

    sheets = sheets_client.get_sheets_service()
    sheet_rows = sheets_client.fetch_all_rows(sheets)
    directory_rows = company_directory.load()
    seen = state.load_seen_career_postings()

    scannable = [
        r for r in directory_rows
        if r.get("Link", "").strip()
        and "disqualified" not in r.get("Notes", "").lower()
        and company_directory.NO_LINK_FOUND_MARKER not in r.get("Notes", "").lower()
    ]
    logger.info("Scanning %d tracked compan(y/ies) with a usable link%s.", len(scannable), " (dry-run)" if dry_run else "")

    new_match_count = 0
    js_rendered_count = 0
    fetch_failed_count = 0

    for row in scannable:
        company, link = row["Company Name"].strip(), row["Link"].strip()
        time.sleep(_PACE_SECONDS)
        html = job_page_fetcher.fetch_raw_html(link)
        if not html:
            fetch_failed_count += 1
            logger.debug("[FETCH FAILED] '%s' -> %s unreachable.", company, link)
            continue

        candidates = job_page_fetcher.extract_job_links(html, link)
        if not candidates:
            js_rendered_count += 1
            logger.debug("[NO LINKS] '%s' -> likely JS-rendered listing page, nothing extractable.", company)
            continue

        already_seen = set(seen.get(company, []))
        new_links = [(title, url) for title, url in candidates if url not in already_seen]
        if not new_links:
            continue

        seen.setdefault(company, [])
        for title, url in new_links:
            seen[company].append(url)  # mark seen regardless of outcome -- never re-considered

            if classifier.is_junior_or_intern_title(title):
                logger.info("[SKIP] '%s @ %s' -> junior/intern/student title, not relevant.", title, company)
                continue

            time.sleep(_PACE_SECONDS)
            try:
                posting = job_page_fetcher.fetch_generic_posting(url)
            except Exception:
                logger.exception("Failed fetching candidate '%s @ %s' -> skipping", title, company)
                continue
            if not posting.description or posting.closed:
                continue  # never invent -- no real content means no finding, not an error

            resolved_company = classifier.confirm_company_name(posting.description, company)
            jid = main.job_id_for(resolved_company, title, position_id=_position_id_from_url(url))
            if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
                continue  # already tracked (e.g. this posting arrived by email first)

            try:
                score_result = cv_matcher.score_job_email(resolved_company, title, posting.description[:config.DESCRIPTION_SCORE_CHARS])
            except Exception:
                logger.exception("Scoring failed for '%s @ %s'", title, resolved_company)
                continue
            score = score_result.get("score", -1)

            if score < config.FIT_SCORE_THRESHOLD:
                logger.info("[LOW FIT] '%s @ %s' score=%s < threshold, skipping.", title, resolved_company, score)
                state.log_skipped_candidate(
                    resolved_company, title, "LOW_FIT (career-page scan)", score, url, score_result.get("summary", "")
                )
                continue

            new_match_count += 1
            today = datetime.now().strftime("%Y-%m-%d")
            logger.info("[NEW MATCH] '%s @ %s' score=%s -> appending row + notifying.", title, resolved_company, score)
            notes = sheets_client.append_status_history(score_result.get("summary", ""), config.STATUS_NOT_APPLIED_YET, today)
            row_number = -1
            if not dry_run:
                row_number = position_sheet.append_position(sheets, position_sheet.PositionRecord(
                    company=resolved_company, title=title, status=config.STATUS_NOT_APPLIED_YET, date_saved=today,
                    url=url, description=posting.description[:config.DESCRIPTION_STORE_CHARS],
                    notes=notes, job_id=jid, fit_score=score,
                ))
                notifier.notify_new_match(resolved_company, title, score)
            sheet_rows.append({
                **dict.fromkeys(config.SHEET_COLUMNS, ""), "company": resolved_company, "job_id": jid,
                "status": config.STATUS_NOT_APPLIED_YET, "_row": row_number,
            })

    if not dry_run:
        state.save_seen_career_postings(seen)
        sheets_client.refresh_basic_filter(sheets)

    summary = (
        f"Career-page scan: {new_match_count} new match(es), "
        f"{js_rendered_count} unscannable (JS-rendered), {fetch_failed_count} fetch failure(s) "
        f"across {len(scannable)} compan(y/ies)."
    )
    logger.info("[RUN SUMMARY] %s", summary)
    if new_match_count and not dry_run:
        notifier.notify_needs_review(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan tracked companies' career pages for new postings")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions without writing")
    args = parser.parse_args()

    setup_logging()
    run(dry_run=args.dry_run)
