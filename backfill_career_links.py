"""Retrofits tracked rows that are missing url/description (e.g. created from an
application-reply email where the original job-opportunity email was never seen) by
searching the company's career site for the matching position.

Usage:
    python backfill_career_links.py             # backfills url/description/fit_score
    python backfill_career_links.py --dry-run    # logs intended actions only
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

import config
import cv_matcher
import notifier
import position_resolver
import sheets_client

logger = logging.getLogger(__name__)

_PACE_SECONDS = 2


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"backfill_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def run(dry_run: bool = False) -> None:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set. Set the JOBACE_SHEET_ID env var or edit config.py.")
        return

    sheets = sheets_client.get_sheets_service()
    rows = sheets_client.fetch_all_rows(sheets)

    candidates = [
        r for r in rows
        if not r.get("url", "").strip() and not r.get("description", "").strip()
        and r.get("company", "").strip() and not sheets_client.is_row_closed(r)
        and not sheets_client.is_row_not_relevant(r)
    ]
    logger.info("Found %d candidate row(s) missing url/description%s.", len(candidates), " (dry-run)" if dry_run else "")

    given_up_count = 0
    for row in candidates:
        time.sleep(_PACE_SECONDS)
        company, title, row_number = row.get("company", ""), row.get("title", ""), row["_row"]
        try:
            resolved = position_resolver.resolve_position(company, title)
        except Exception:
            logger.exception("Failed resolving '%s @ %s' -> leaving unchanged", title, company)
            continue

        if not resolved.url and not resolved.description:
            attempt = sheets_client.parse_attempt_count(row.get("notes", "")) + 1
            if attempt >= config.MAX_RESOLUTION_ATTEMPTS and sheets_client.is_eligible_for_closure(row):
                given_up_count += 1
                logger.info(
                    "[GIVING UP] '%s @ %s' -> unresolved after %d attempts, marking nr.",
                    title, company, attempt,
                )
                if not dry_run:
                    sheets_client.update_row_fields(sheets, row_number, {
                        "status": "nr",
                        "notes": f"Could not verify a real posting description after {attempt} attempts. Giving up automatically.",
                    })
            elif attempt >= config.MAX_RESOLUTION_ATTEMPTS:
                logger.info(
                    "[GIVING UP] '%s @ %s' -> unresolved after %d attempts, but status is '%s' (real pipeline history) -- leaving status untouched, not marking nr.",
                    title, company, attempt, row.get("status", ""),
                )
                if not dry_run:
                    sheets_client.update_row_fields(sheets, row_number, {
                        "notes": f"Could not verify a real posting description after {attempt} attempts. Not auto-marking nr since status is '{row.get('status', '')}'.",
                    })
            else:
                logger.info("[NO MATCH] '%s @ %s' -> attempt %d/%d.", title, company, attempt, config.MAX_RESOLUTION_ATTEMPTS)
                if not dry_run:
                    sheets_client.update_row_fields(sheets, row_number, {
                        "notes": sheets_client.format_attempt_note(
                            "Could not verify a real posting description.", attempt, config.MAX_RESOLUTION_ATTEMPTS
                        ),
                    })
            continue

        fields = {}
        if resolved.url:
            fields["url"] = resolved.url
        if resolved.description:
            fields["description"] = resolved.description
            try:
                score_result = cv_matcher.score_job_email(resolved.company, title, resolved.description[:config.DESCRIPTION_SCORE_CHARS])
                fields["fit_score"] = score_result.get("score", "")
            except Exception:
                logger.exception("Scoring failed for '%s @ %s'", title, company)
        if resolved.company != company:
            fields["company"] = resolved.company

        logger.info("[BACKFILLED] row %s '%s @ %s' -> %s", row_number, title, company, fields)
        if not dry_run:
            sheets_client.update_row_fields(sheets, row_number, fields)

    if given_up_count:
        summary = f"{given_up_count} row(s) gave up after {config.MAX_RESOLUTION_ATTEMPTS} backfill attempts (now 'nr')."
        logger.warning("[RUN SUMMARY] %s", summary)
        notifier.notify_needs_review(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill missing url/description via career-site search")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions without writing")
    args = parser.parse_args()

    setup_logging()
    run(dry_run=args.dry_run)
