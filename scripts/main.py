"""Local Mail Agent — one run = one batch-check cycle. Intended for Task Scheduler every 5-10 min.

Orchestrates 5 pipeline stages (see src/mail_agent/pipeline/): fetch (this file) ->
extract -> verify -> score -> reconcile. Each stage is a separately-tested, typed
function in its own module -- a bug is always attributable to exactly one stage,
which can be reproduced and fixed in isolation (see each stage's own docstring).

Usage:
    python main.py             # real run: marks read, updates/appends sheet rows, notifies
    python main.py --dry-run   # logs intended actions only, no writes
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config
from mail_agent import gmail_client
from mail_agent import notifier
from mail_agent import sheets_client
from mail_agent import state
from mail_agent.pipeline import extract
from mail_agent.pipeline import reconcile
from mail_agent.pipeline import score
from mail_agent.pipeline import verify
from mail_agent.pipeline.dedup import job_id_for, next_status  # noqa: F401  (re-exported for existing callers/tests)
from mail_agent.pipeline.types import Candidate

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    log_path = os.path.join(config.LOGS_DIR, f"agent_{datetime.now():%Y-%m-%d}.log")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def run(dry_run: bool = False) -> bool:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set. Set the JOBACE_SHEET_ID env var or edit config.py.")
        return False

    gmail = gmail_client.get_gmail_service()
    sheets = sheets_client.get_sheets_service()
    if not dry_run:
        sheets_client.ensure_header(sheets)

    processed = state.load_processed_ids()
    all_ids = gmail_client.list_recent_ids(gmail, newer_than_days=config.MAIL_LOOKBACK_DAYS)
    new_ids = [i for i in all_ids if i not in processed]
    # Full message content (body, headers) is only fetched for genuinely new IDs --
    # the label routinely holds several hundred messages within the lookback window,
    # so fetching every one's full body every run would be wasteful and can trip
    # Gmail's per-minute quota (confirmed while first testing this).
    new_messages = [gmail_client.fetch_message(gmail, i) for i in new_ids]

    retry_state = state.load_pending_verification()
    pending_candidates = verify.pending_candidates(retry_state)

    logger.info(
        "[PIPELINE] Fetched %d from Work label, %d new message(s), %d pending retry candidate(s)%s.",
        len(all_ids), len(new_messages), len(pending_candidates), " (dry-run)" if dry_run else "",
    )

    if not new_messages and not pending_candidates:
        if not dry_run and os.path.exists(config.MAIL_QUEUE_INCOMPLETE_FLAG):
            os.remove(config.MAIL_QUEUE_INCOMPLETE_FLAG)
        return True

    sheet_rows = sheets_client.fetch_all_rows(sheets)

    stopped_early = False
    consecutive_failures = 0
    ambiguous_count = 0
    # mail_id -> list[bool], one entry per candidate extracted from that message;
    # True = "nothing changed for this candidate". A message is marked read only if
    # every one of its candidates produced no change (matches the original
    # semantics: new matches and status updates stay unread as a visible signal).
    per_message_no_change: dict[str, list[bool]] = {}
    # mail_id -> False once extraction itself raises for that message -- such a
    # message is never marked processed/read, so it's retried next run exactly
    # like a stage-3/4/5 failure would be.
    extraction_ok: dict[str, bool] = {}

    all_candidates: list[Candidate] = list(pending_candidates)
    for msg in new_messages:
        if stopped_early:
            break
        try:
            candidates = extract.extract_candidates(msg, sheet_rows)
            extraction_ok[msg.id] = True
            all_candidates.extend(candidates)
            consecutive_failures = 0
        except Exception:
            logger.exception("[PIPELINE] Failed extracting from message id=%s subject=%r -> will retry next run", msg.id, msg.subject)
            extraction_ok[msg.id] = False
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.warning(
                    "3 consecutive failures (Ollama unreachable or erroring) -> "
                    "stopping this run early, remaining messages will retry next cycle."
                )
                stopped_early = True

    stage_counts = Counter()
    if not stopped_early:
        for c in all_candidates:
            try:
                if c.kind == "opportunity":
                    verified = verify.verify_position(c, sheet_rows, retry_state)
                    if verified is None:
                        stage_counts["verify_failed"] += 1
                        per_message_no_change.setdefault(c.mail_id, []).append(True)
                        continue
                    scored = score.score_position(verified)
                    if scored is None:
                        stage_counts["low_fit"] += 1
                        per_message_no_change.setdefault(c.mail_id, []).append(True)
                        continue
                    result = reconcile.reconcile(scored, sheet_rows, sheets, dry_run)
                else:
                    result = reconcile.reconcile(c, sheet_rows, sheets, dry_run)
                    if result.action == "ambiguous":
                        ambiguous_count += 1
                stage_counts[result.action] += 1
                per_message_no_change.setdefault(c.mail_id, []).append(result.action not in ("inserted", "updated"))
                consecutive_failures = 0
            except Exception:
                logger.exception(
                    "[PIPELINE] Failed reconciling candidate mail=%s company=%r title=%r -> will retry next run",
                    c.mail_id, c.company, c.title,
                )
                per_message_no_change.setdefault(c.mail_id, []).append(False)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    logger.warning(
                        "3 consecutive failures (Ollama unreachable or erroring) -> "
                        "stopping this run early, remaining candidates will retry next cycle."
                    )
                    stopped_early = True
                    break

    logger.info(
        "[PIPELINE] %d candidate(s) processed: %s", len(all_candidates),
        dict(stage_counts) if stage_counts else "(none reached verify/reconcile)",
    )

    if not dry_run:
        state.save_pending_verification(retry_state)

    for msg in new_messages:
        if extraction_ok.get(msg.id) is False:
            continue  # extraction itself failed -- retry this message next run
        no_change_flags = per_message_no_change.get(msg.id, [])
        mark_read = all(no_change_flags) if no_change_flags else True
        if mark_read:
            logger.info("-> mark as read (no sheet change) [mail=%s]", msg.id)
            if not dry_run:
                gmail_client.mark_as_read(gmail, msg.id)
        processed.add(msg.id)

    if not dry_run:
        state.save_processed_ids(processed)
        sheets_client.refresh_basic_filter(sheets)
        if stopped_early:
            with open(config.MAIL_QUEUE_INCOMPLETE_FLAG, "w", encoding="utf-8") as f:
                f.write(f"Stopped early at {datetime.now().isoformat()} -- mail queue not fully drained.")
        elif os.path.exists(config.MAIL_QUEUE_INCOMPLETE_FLAG):
            os.remove(config.MAIL_QUEUE_INCOMPLETE_FLAG)

    if ambiguous_count:
        summary = f"{ambiguous_count} ambiguous status update(s) this run -- check [NEEDS REVIEW] log lines."
        logger.warning("[RUN SUMMARY] %s", summary)
        notifier.notify_needs_review(summary)

    return not stopped_early


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Mail Agent batch run")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions without writing")
    args = parser.parse_args()

    setup_logging()
    # Real incident: two overlapping runs (manual + bat) raced on the same messages and
    # inserted every position twice. A lock file stops a second instance instead.
    lock_path = os.path.join(config.DATA_DIR, "main.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - os.path.getmtime(lock_path) < 4 * 3600:
            logger.error("Another run is in progress (%s). Exiting.", lock_path)
            sys.exit(1)
        os.remove(lock_path)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        run(dry_run=args.dry_run)
    finally:
        os.close(lock_fd)
        os.remove(lock_path)
