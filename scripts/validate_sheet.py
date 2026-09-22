"""Post-run integrity check over the whole jobAce sheet -- catches rows that other
scripts already tried to avoid writing badly, but that could still exist from before
those guards existed (or from manual edits): missing company/title, or an active row
stuck with no url/description past the point backfill_career_links.py gives up on it.

This never edits rows -- it only reports, so nothing gets silently "fixed" wrong.
Run at the end of run_mail_agent.bat; findings go to the log and one summary
notification (not one toast per row) rather than requiring a manual sheet read-through.

Usage:
    python validate_sheet.py
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config
from mail_agent import guardrails
from mail_agent import notifier
from mail_agent import sheets_client

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"validate_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def _missing_content(row: dict) -> bool:
    """An active, trackable row (not closed/nr) with neither a link nor a description
    is unreviewable -- there's nothing to look at to judge fit or find the posting."""
    if sheets_client.is_row_closed(row) or sheets_client.is_row_not_relevant(row):
        return False
    return not row.get("url", "").strip() and not row.get("description", "").strip()


def run() -> dict:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set.")
        return {}

    sheets = sheets_client.get_sheets_service()
    rows = sheets_client.fetch_all_rows(sheets)

    identity_problems = []
    content_problems = []
    for row in rows:
        missing = guardrails.missing_identity_fields(row)
        if missing:
            identity_problems.append((row["_row"], missing, row))
        elif _missing_content(row):
            attempts = sheets_client.parse_attempt_count(row.get("notes", ""))
            content_problems.append((row["_row"], attempts, row))

    for row_number, missing, row in identity_problems:
        logger.warning(
            "[VALIDATE] row %s missing %s -- company=%r title=%r status=%r",
            row_number, "/".join(missing), row.get("company", ""), row.get("title", ""), row.get("status", ""),
        )

    for row_number, attempts, row in content_problems:
        logger.warning(
            "[VALIDATE] row %s '%s @ %s' has no url or description (status=%r, resolution attempts=%d)",
            row_number, row.get("title", ""), row.get("company", ""), row.get("status", ""), attempts,
        )

    total = len(identity_problems) + len(content_problems)
    if total:
        summary = (
            f"Sheet validation: {len(identity_problems)} row(s) missing company/title, "
            f"{len(content_problems)} active row(s) with no url/description. See validate_*.log for row numbers."
        )
        logger.warning("[RUN SUMMARY] %s", summary)
        notifier.notify_needs_review(summary)
    else:
        logger.info("[VALIDATE] No issues found across %d row(s).", len(rows))

    return {
        "identity_problems": [r for r, _, _ in identity_problems],
        "content_problems": [r for r, _, _ in content_problems],
    }


if __name__ == "__main__":
    setup_logging()
    run()
