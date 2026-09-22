"""Local Mail Agent — one run = one batch-check cycle. Intended for Task Scheduler every 5-10 min.

Usage:
    python main.py             # real run: marks read, updates/appends sheet rows, notifies
    python main.py --dry-run   # logs intended actions only, no writes
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import classifier
from mail_agent import config
from mail_agent import cv_matcher
from mail_agent import gmail_client
from mail_agent import job_page_fetcher
from mail_agent import notifier
from mail_agent import position_resolver
from mail_agent import position_sheet
from mail_agent import sheets_client
from mail_agent import state

logger = logging.getLogger(__name__)

# Reset at the start of every run() call; counts things that need a human to look at
# them so a one-line summary can be surfaced at the end instead of relying on
# catching an individual toast notification mid-run.
_run_stats = {"ambiguous": 0}


def next_status(old_status: str, signal: str) -> str:
    """Compute the granular status from the raw LLM signal (applied/interview/offer/
    rejected) plus the row's current status -- distinguishes an early/automated
    rejection (never got an interview) from a post-interview rejection, and never
    lets a stale "applied" ack downgrade a status that's already progressed further."""
    old = (old_status or "").strip().lower()
    if signal == "rejected":
        return "reject" if old == "interview" else "ATS_reject"
    if signal == "interview":
        return "interview"
    if signal == "offer":
        return "offer"
    if signal == "applied":
        return old_status if old in ("interview", "offer", "reject", "atsreject", "ats_reject") else "applied"
    return old_status


def job_id_for(company: str, title: str, position_id: str = "", requisition_id: str = "") -> str:
    """Dedup key priority:
    1. Employer's own requisition ID (e.g. "JR2023080"), when the real fetched job
       description contains one -- the MOST stable identifier, since a role that
       closes and reopens gets a brand new LinkedIn listing ID but keeps this one.
    2. LinkedIn's own numeric listing ID -- stable for as long as that specific
       listing stays open.
    3. Company + noise-stripped normalized title (exact match, not fuzzy) when
       neither ID is available, to avoid false-positive dedup between genuinely
       different roles at the same company.
    Company is deliberately NOT part of the key for 1/2, since the same real posting
    can get extracted with a slightly different company-name spelling."""
    if requisition_id.strip():
        key = f"req:{requisition_id.strip().lower()}"
    elif position_id.strip():
        key = f"pid:{position_id.strip().lower()}"
    else:
        key = f"{company.strip().lower()}|{sheets_client.normalize_title(title)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


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


def run(dry_run: bool = False) -> None:
    if not config.SHEET_ID:
        logger.error("config.SHEET_ID is not set. Set the JOBACE_SHEET_ID env var or edit config.py.")
        return

    _run_stats["ambiguous"] = 0

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

    logger.info("Fetched %d from Work label, %d new to process%s.",
                len(all_ids), len(new_messages), " (dry-run)" if dry_run else "")

    if not new_messages:
        if not dry_run and os.path.exists(config.MAIL_QUEUE_INCOMPLETE_FLAG):
            os.remove(config.MAIL_QUEUE_INCOMPLETE_FLAG)
        return True

    sheet_rows = sheets_client.fetch_all_rows(sheets)

    stopped_early = False
    consecutive_failures = 0
    for msg in new_messages:
        try:
            mark_read = _handle_message(msg, gmail, sheets, sheet_rows, dry_run=dry_run)
            if mark_read:
                logger.info("-> mark as read (no sheet change)")
                if not dry_run:
                    gmail_client.mark_as_read(gmail, msg.id)
            processed.add(msg.id)
            consecutive_failures = 0
        except Exception:
            logger.exception(
                "Failed handling message id=%s subject=%r -> will retry next run", msg.id, msg.subject
            )
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.warning(
                    "3 consecutive failures (Ollama unreachable or erroring) -> "
                    "stopping this run early, remaining messages will retry next cycle."
                )
                stopped_early = True
                break

    if not dry_run:
        state.save_processed_ids(processed)
        sheets_client.refresh_basic_filter(sheets)
        if stopped_early:
            with open(config.MAIL_QUEUE_INCOMPLETE_FLAG, "w", encoding="utf-8") as f:
                f.write(f"Stopped early at {datetime.now().isoformat()} -- mail queue not fully drained.")
        elif os.path.exists(config.MAIL_QUEUE_INCOMPLETE_FLAG):
            os.remove(config.MAIL_QUEUE_INCOMPLETE_FLAG)

    if _run_stats["ambiguous"]:
        summary = f"{_run_stats['ambiguous']} ambiguous status update(s) this run -- check [NEEDS REVIEW] log lines."
        logger.warning("[RUN SUMMARY] %s", summary)
        notifier.notify_needs_review(summary)

    return not stopped_early


def _handle_digest_posting(posting: dict, sheets, sheet_rows: list[dict], dry_run: bool, date_utc: str) -> bool:
    """Handles one posting parsed out of a LinkedIn digest email. Returns True if
    nothing changed in the sheet for this posting (used to decide the overall
    message's read state -- see _handle_message)."""
    company = posting["company"]
    title = posting["title"]
    if classifier.is_platform_company_name(company):
        return True
    if classifier.is_junior_or_intern_title(title):
        logger.info("[SKIP] '%s @ %s' -> junior/intern/student title, not relevant.", title, company)
        return True
    if classifier.is_location_excluded(posting["location"]):
        logger.info("[SKIP] '%s @ %s' -> location '%s' not commutable, not relevant.", title, company, posting["location"])
        return True

    # Stage 1: cheap dedup by LinkedIn's own listing ID, before spending a fetch.
    # Only checked against active rows -- a closed row's job_id must not block a
    # fresh listing (e.g. the same requisition reopened) from being added as new.
    pid_jid = job_id_for(company, title, posting["position_id"])
    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), pid_jid):
        logger.info("[DUP] '%s @ %s' already tracked (listing ID), skipping.", title, company)
        return True

    fetched = job_page_fetcher.fetch_linkedin_posting(posting["url"]) if posting["url"] else job_page_fetcher.FetchedPosting()
    if fetched.closed:
        logger.info("[CLOSED] '%s @ %s' -> posting no longer accepting applications, skipping.", title, company)
        return True
    description = fetched.description
    if not description:
        # Never score/decide fitness without a real job description -- there is no
        # honest basis for a match/no-match judgment from title alone. Skip entirely
        # rather than invent "typical requirements" for this title and present that
        # guess as if it were a real assessment.
        logger.info(
            "[SKIP] '%s @ %s' -> could not fetch job description, not scoring (no invented requirements).",
            title, company,
        )
        return True

    # Stage 2: stronger dedup by the employer's own requisition ID, now that we have
    # the real description -- catches a role that closed and reopened under a new
    # LinkedIn listing ID (same requisition, different pid_jid from stage 1).
    requisition_id = classifier.extract_requisition_id(description)
    jid = job_id_for(company, title, posting["position_id"], requisition_id)
    if requisition_id and sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[DUP] '%s @ %s' already tracked (requisition ID %s), skipping.", title, company, requisition_id)
        return True

    # Stage 3: exact-description dedup -- ONLY when no requisition ID was stated at
    # all. A real employer-stated job/requisition ID is always the best signal for
    # distinguishing positions, even between two postings with identical company,
    # title, and boilerplate-identical description text (e.g. several genuinely
    # distinct openings for the same role) -- if one exists, trust it and never
    # let description matching override it. This stage exists only for the case a
    # requisition ID proved unavailable (LinkedIn's own listing ID alone is not
    # reliable evidence of distinctness -- it syndicated one real Amazon posting
    # under two different listing IDs with a byte-identical description and no
    # stated requisition ID, which neither stage above caught).
    if not requisition_id and sheets_client.find_row_by_company_and_description(sheets_client.active_rows(sheet_rows), company, description[:config.DESCRIPTION_STORE_CHARS]):
        logger.info("[DUP] '%s @ %s' already tracked (identical description, no requisition ID stated), skipping.", title, company)
        return True

    try:
        score_result = cv_matcher.score_job_email(company, title, description[:config.DESCRIPTION_SCORE_CHARS])
    except Exception:
        logger.exception("Scoring failed for digest posting '%s @ %s'", title, company)
        raise
    score = score_result.get("score", -1)

    if score < config.FIT_SCORE_THRESHOLD:
        logger.info("[LOW FIT] '%s @ %s' score=%s < threshold, skipping sheet write.", title, company, score)
        state.log_skipped_candidate(
            company, title, "LOW_FIT", score, posting["url"], score_result.get("summary", "")
        )
        return True

    logger.info("[NEW MATCH] '%s @ %s' score=%s -> appending row + notifying.", title, company, score)
    notes = sheets_client.append_status_history(score_result.get("summary", ""), config.STATUS_NOT_APPLIED_YET, date_utc)
    row_number = -1
    if not dry_run:
        row_number = position_sheet.append_position(sheets, position_sheet.PositionRecord(
            company=company, title=title, status=config.STATUS_NOT_APPLIED_YET, date_saved=date_utc,
            url=posting["url"], location=posting["location"],
            description=description[:config.DESCRIPTION_STORE_CHARS], notes=notes,
            job_id=jid, fit_score=score,
        ))
        notifier.notify_new_match(company, title, score)
    sheet_rows.append({
        **dict.fromkeys(config.SHEET_COLUMNS, ""), "company": company, "job_id": jid,
        "status": config.STATUS_NOT_APPLIED_YET, "_row": row_number,
    })
    return False


def _handle_generic_digest_posting(posting: dict, sheets, sheet_rows: list[dict], dry_run: bool, date_utc: str) -> bool:
    """Handles one posting parsed out of a generic (non-LinkedIn) multi-position
    digest -- e.g. an Indeed/Glassdoor "N new jobs" alert. Mirrors
    _handle_digest_posting's filters/dedup/scoring, but description comes from the
    digest's own verbatim snippet text (real content already visible in the email)
    rather than a LinkedIn-specific fetch -- Indeed/Glassdoor postings are known to
    401-block direct scraping, so fetch_generic_posting is only a best-effort topper,
    never the only source. Returns True if nothing changed in the sheet."""
    company, title = posting["company"], posting["title"]
    if classifier.is_platform_company_name(company):
        return True
    if classifier.is_junior_or_intern_title(title):
        logger.info("[SKIP] '%s @ %s' -> junior/intern/student title, not relevant.", title, company)
        return True
    if classifier.is_location_excluded(posting["location"]):
        logger.info("[SKIP] '%s @ %s' -> location '%s' not commutable, not relevant.", title, company, posting["location"])
        return True

    jid = job_id_for(company, title)
    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[DUP] '%s @ %s' already tracked, skipping.", title, company)
        return True

    description = posting.get("snippet", "").strip()
    if len(description) < position_resolver.MIN_CONTENT_LENGTH and posting.get("url"):
        try:
            fetched = job_page_fetcher.fetch_generic_posting(posting["url"])
        except Exception:
            fetched = job_page_fetcher.FetchedPosting()
        if fetched.description and not fetched.closed:
            description = fetched.description

    if len(description) < position_resolver.MIN_CONTENT_LENGTH:
        # Never score/decide fitness without real content -- a digest snippet this
        # short (or a fetch that failed, e.g. Indeed's known 401 block) isn't an
        # honest basis for a match/no-match judgment.
        logger.info(
            "[SKIP] '%s @ %s' -> no real description available (digest snippet too short, fetch failed), not scoring.",
            title, company,
        )
        return True

    try:
        score_result = cv_matcher.score_job_email(company, title, description[:config.DESCRIPTION_SCORE_CHARS])
    except Exception:
        logger.exception("Scoring failed for generic digest posting '%s @ %s'", title, company)
        raise
    score = score_result.get("score", -1)

    if score < config.FIT_SCORE_THRESHOLD:
        logger.info("[LOW FIT] '%s @ %s' score=%s < threshold, skipping sheet write.", title, company, score)
        state.log_skipped_candidate(
            company, title, "LOW_FIT", score, posting.get("url", ""), score_result.get("summary", "")
        )
        return True

    logger.info("[NEW MATCH] '%s @ %s' score=%s -> appending row + notifying.", title, company, score)
    notes = sheets_client.append_status_history(score_result.get("summary", ""), config.STATUS_NOT_APPLIED_YET, date_utc)
    row_number = -1
    if not dry_run:
        row_number = position_sheet.append_position(sheets, position_sheet.PositionRecord(
            company=company, title=title, status=config.STATUS_NOT_APPLIED_YET, date_saved=date_utc,
            url=posting.get("url", ""), location=posting["location"],
            description=description[:config.DESCRIPTION_STORE_CHARS], notes=notes,
            job_id=jid, fit_score=score,
        ))
        notifier.notify_new_match(company, title, score)
    sheet_rows.append({
        **dict.fromkeys(config.SHEET_COLUMNS, ""), "company": company, "job_id": jid,
        "status": config.STATUS_NOT_APPLIED_YET, "_row": row_number,
    })
    return False


def _apply_status_signal(
    sheets, matched_row: dict, company: str, triage: dict, dry_run: bool, date_utc: str, match_tier: str = "unknown"
) -> bool:
    """Applies a triage-derived status signal to an already-matched row. Returns True
    if nothing changed (no signal in this triage), False if the row was updated.
    `match_tier` is logged alongside the update purely for auditability -- when a
    wrong-row incident like the Mobileye/Maytronics ones happens again, the log
    should immediately say which tier justified the match instead of requiring a
    from-scratch reproduction to find out."""
    signal = triage.get("status", "")
    if not signal:
        return True
    old_status = matched_row.get("status", "")
    status = next_status(old_status, signal)
    logger.info(
        "[STATUS UPDATE] %s -> status=%s (was %s, signal=%s, row %s, matched_via=%s)",
        company, status, old_status, signal, matched_row.get("_row"), match_tier,
    )
    if not dry_run:
        fields = {"status": status}
        if triage.get("contact_name"):
            fields["contact_name"] = triage["contact_name"]
        notes = matched_row.get("notes", "")
        if triage.get("notes"):
            notes = (notes + " | " + triage["notes"]).strip(" |")
        # Record whenever real new information arrived: either the status itself
        # changed, or it's an "interview" signal -- next_status always returns
        # "interview" regardless of prior status, so a 2nd/3rd interview round would
        # otherwise look like a no-op and get silently dropped even though it's a
        # genuine new event worth a date. A repeated "applied"/"offer" ack with no
        # status change is the only case genuinely uninteresting to log.
        if status != old_status or signal == "interview":
            notes = sheets_client.append_status_history(notes, status, date_utc)
        fields["notes"] = notes
        sheets_client.update_row_fields(sheets, matched_row["_row"], fields)
    return False


def _resolve_reply_target_row(sheet_rows: list[dict], company: str, title: str) -> tuple[dict | None, list[dict], str]:
    """Finds which tracked row an application-reply/status email is about. Returns
    (row, ambiguous_candidates, match_tier). Exactly one row for the company is
    unambiguous and returned even without a title match (the common case: one
    tracked role per company). With multiple rows, a title match disambiguates;
    failing that, returns (None, candidates, "") rather than guessing -- silently
    picking "first row for this company" is exactly what wrote an "applied" status
    to the wrong Mobileye row (and, combined with a company-name mismatch, created a
    stray duplicate for Micron/"Micron Technology") before this guard existed."""
    title_match = sheets_client.find_row_by_company_and_title(sheet_rows, company, title)
    if title_match:
        return title_match, [], "title_match"
    candidates = sheets_client.find_rows_by_company(sheet_rows, company)
    if len(candidates) <= 1:
        return (candidates[0] if candidates else None), [], "single_company_row"
    return None, candidates, ""


def _flag_ambiguous_status_update(company: str, title: str, triage: dict, candidates: list[dict]) -> None:
    _run_stats["ambiguous"] += 1
    row_numbers = [c["_row"] for c in candidates]
    logger.warning(
        "[NEEDS REVIEW] '%s' status signal (title=%r, signal=%s) matches %d tracked rows for this "
        "company (rows %s) -- ambiguous, not auto-applying. Update manually.",
        company, title, triage.get("status", ""), len(candidates), row_numbers,
    )
    notifier.notify_needs_review(
        f"Ambiguous status update for {company} ({triage.get('status', '')}) -- "
        f"matches rows {row_numbers}, please update manually."
    )


def _handle_possible_status_update(msg, posting: dict, sheets, sheet_rows: list[dict], dry_run: bool) -> bool:
    """A digest-shaped body (Title\\nCompany\\nLocation\\nView job: <url>) whose job_id
    is already tracked isn't a new opportunity -- it's some kind of status
    notification about that existing application (LinkedIn's own "application
    sent/viewed", or an employer/ATS reply -- e.g. a rejection -- that happens to
    share this same body shape). Route it through the normal triage + status
    pipeline instead of letting digest dedup silently swallow it."""
    triage = classifier.classify_email(msg)
    company = posting["company"] or triage.get("company", "")
    if triage.get("category") != "application_reply" or not triage.get("status"):
        return True
    jid = job_id_for(company, posting["title"], posting["position_id"])
    matched_row = sheets_client.find_row_by_job_id(sheet_rows, jid)
    match_tier = "job_id"
    if not matched_row:
        matched_row, ambiguous, match_tier = _resolve_reply_target_row(sheet_rows, company, posting["title"])
        if ambiguous:
            _flag_ambiguous_status_update(company, posting["title"], triage, ambiguous)
            return False
    if not matched_row:
        return True
    return _apply_status_signal(sheets, matched_row, company, triage, dry_run, msg.date_utc, match_tier)


def _handle_message(msg, gmail, sheets, sheet_rows: list[dict], dry_run: bool) -> bool:
    """Returns True if the message should be marked as read (i.e. nothing new was
    added/updated in the sheet -- new matches and status updates stay unread as a
    visible signal). Promotional mail is handled entirely by the Gmail Apps Script
    (moveCommercialsToSpam), not here -- this agent only sees the Work label anyway."""
    if classifier.is_connection_request(msg):
        logger.info("[SKIP] '%s' -> LinkedIn connection request, not a job opportunity", msg.subject)
        return True

    digest_postings = classifier.parse_linkedin_digest(msg.body)
    if digest_postings:
        results = []
        for p in digest_postings:
            pid_jid = job_id_for(p["company"], p["title"], p["position_id"])
            already_tracked = sheets_client.find_row_by_job_id(sheet_rows, pid_jid)
            # Guard against "you might also like" recommendations appended below a
            # real single-posting notification (e.g. a LinkedIn "application sent"
            # email) -- those share the same digest body shape and can incidentally
            # match an already-tracked job_id, but the confirmation/status content is
            # about the company actually named in the subject, not these unrelated
            # extras. Only route to status-update handling when the subject itself
            # names this posting's company.
            is_subject_target = p["company"].strip().lower() in msg.subject.lower()
            if already_tracked and is_subject_target:
                results.append(_handle_possible_status_update(msg, p, sheets, sheet_rows, dry_run))
            else:
                results.append(_handle_digest_posting(p, sheets, sheet_rows, dry_run, msg.date_utc))
        return all(results)

    triage = classifier.classify_email(msg)
    category = triage.get("category", "other")
    company = triage.get("company", "")

    if category == "application_reply" and company and not classifier.is_platform_company_name(company):
        matched_row, ambiguous, match_tier = _resolve_reply_target_row(sheet_rows, company, triage.get("role_title", ""))
        if ambiguous:
            _flag_ambiguous_status_update(company, triage.get("role_title", ""), triage, ambiguous)
            return False
        if matched_row:
            return _apply_status_signal(sheets, matched_row, company, triage, dry_run, msg.date_utc, match_tier)

        # No existing row for this company: write what we have rather than discard it
        # (a reply implies an application already happened, even if we never saw the
        # original opportunity email -- e.g. it was applied to directly on a career site).
        # Falls back to the email subject when the triage LLM couldn't extract a role
        # title from a terse confirmation body -- mirrors the job_opportunity path
        # below (line ~529); leaving this blank produced an untitled, unidentifiable
        # row (company only) that nothing could ever look up or resolve later.
        title = triage.get("role_title") or msg.subject or ""
        status = next_status("", triage.get("status", "")) if triage.get("status") else "applied"
        jid = job_id_for(company, title or company, triage.get("position_id", ""))
        if sheets_client.find_row_by_job_id(sheet_rows, jid):
            logger.info("[DUP] application reply for '%s @ %s' already tracked, skipping.", title, company)
            return True
        logger.info("[NEW FROM REPLY] company=%s title=%r status=%s -> creating row.", company, title, status)
        row_number = -1
        if not dry_run:
            row_number = position_sheet.append_position(sheets, position_sheet.PositionRecord(
                company=company, title=title, status=status, date_saved=msg.date_utc,
                notes=sheets_client.append_status_history(triage.get("notes", ""), status, msg.date_utc),
                job_id=jid, contact_name=triage.get("contact_name", ""),
            ))
        sheet_rows.append({**dict.fromkeys(config.SHEET_COLUMNS, ""), "company": company, "job_id": jid, "status": status, "_row": row_number})
        return False

    looks_like_unresolved_digest = category != "job_opportunity" or (
        not company and classifier.is_platform_domain(msg.sender_email)
    )
    if looks_like_unresolved_digest:
        # Before giving up: the single-opportunity triage prompt can't extract "the"
        # company from an email that genuinely bundles several (an Indeed/Glassdoor
        # "N new jobs" alert, not just LinkedIn's own digest format) -- try the
        # generic multi-posting extraction as a recovery path rather than silently
        # dropping real postings. Only acts if it confidently finds >=2 distinct real
        # postings; otherwise this is a no-op and the original skip logic below runs
        # unchanged.
        generic_postings = classifier.parse_generic_digest(msg.subject, msg.body)
        if generic_postings:
            results = [
                _handle_generic_digest_posting(p, sheets, sheet_rows, dry_run, msg.date_utc)
                for p in generic_postings
            ]
            return all(results)

    if category != "job_opportunity":
        logger.info("[SKIP] '%s' from %s -> not a job opportunity or known application", msg.subject, msg.sender_email)
        return True

    if classifier.is_platform_company_name(company) or (not company and classifier.is_platform_domain(msg.sender_email)):
        logger.info("[SKIP] '%s' -> job opportunity but no identifiable hiring company (got %r)", msg.subject, company)
        return True

    company = company or msg.sender_name or msg.sender_email
    title = triage.get("role_title") or msg.subject
    location = triage.get("location", "")
    if classifier.is_junior_or_intern_title(title):
        logger.info("[SKIP] '%s @ %s' -> junior/intern/student title, not relevant.", title, company)
        return True
    if classifier.is_location_excluded(location):
        logger.info("[SKIP] '%s @ %s' -> location '%s' not commutable, not relevant.", title, company, location)
        return True
    content = msg.body or msg.snippet
    position_id = classifier.extract_linkedin_job_id(content) or triage.get("position_id", "")
    jid = job_id_for(company, title, position_id)

    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[DUP] '%s @ %s' already tracked, skipping.", title, company)
        return True

    # Re-key on the employer's own requisition ID if this email's content happens to
    # state one (same stability rationale as the digest path).
    requisition_id = classifier.extract_requisition_id(content)
    if requisition_id:
        jid = job_id_for(company, title, position_id, requisition_id)
        if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
            logger.info("[DUP] '%s @ %s' already tracked (requisition ID %s), skipping.", title, company, requisition_id)
            return True

    # Make a real effort to get the actual posting link/description/company rather
    # than ever storing the raw email body as "description" -- see position_resolver.
    # Resolved BEFORE scoring (mirrors the digest path): a fit_score is only ever
    # computed against real, verified position text, never against a generic email
    # body -- and never stored next to a blank description, which would leave no way
    # to see what the score was actually judging.
    resolved = position_resolver.resolve_position(company, title, content)
    if not resolved.description:
        # Never invent a score without real content -- but don't silently drop the
        # opportunity either. A blank-description placeholder row (same pattern as
        # the application-reply "no existing row" case) keeps it visible and makes
        # it a candidate for backfill_career_links.py to keep retrying automatically
        # as tracked_companies.csv improves, instead of losing it forever.
        logger.info(
            "[UNRESOLVED] '%s @ %s' -> could not verify a real posting description; "
            "adding placeholder row instead of dropping it.",
            title, company,
        )
        row_number = -1
        if not dry_run:
            row_number = position_sheet.append_position(sheets, position_sheet.PositionRecord(
                company=company, title=title, status=config.STATUS_NOT_APPLIED_YET, date_saved=msg.date_utc,
                location=location,
                notes=sheets_client.append_status_history(
                    sheets_client.format_attempt_note(
                        "Could not verify a real posting description at ingestion time.", 1, config.MAX_RESOLUTION_ATTEMPTS
                    ),
                    config.STATUS_NOT_APPLIED_YET, msg.date_utc,
                ),
                job_id=jid, contact_name=triage.get("contact_name", ""),
            ))
        sheet_rows.append({
            **dict.fromkeys(config.SHEET_COLUMNS, ""), "company": company, "job_id": jid,
            "status": config.STATUS_NOT_APPLIED_YET, "_row": row_number,
        })
        return False
    if resolved.company != company:
        logger.info("[COMPANY CONFIRMED] '%s' -> '%s' for '%s'", company, resolved.company, title)
        company = resolved.company
        jid = job_id_for(company, title, position_id, requisition_id)
        if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
            logger.info("[DUP] '%s @ %s' already tracked under confirmed company, skipping.", title, company)
            return True

    score_result = cv_matcher.score_job_email(company, title, resolved.description)
    score = score_result.get("score", -1)

    if score < config.FIT_SCORE_THRESHOLD:
        logger.info("[LOW FIT] '%s @ %s' score=%s < threshold, skipping sheet write.", title, company, score)
        state.log_skipped_candidate(
            company, title, "LOW_FIT", score, resolved.url, score_result.get("summary", "")
        )
        return True

    logger.info("[NEW MATCH] '%s @ %s' score=%s -> appending row + notifying.", title, company, score)
    row_number = -1
    if not dry_run:
        row_number = position_sheet.append_position(sheets, position_sheet.PositionRecord(
            company=company, title=title, status=config.STATUS_NOT_APPLIED_YET, date_saved=msg.date_utc,
            url=resolved.url, location=location, description=resolved.description,
            notes=sheets_client.append_status_history(score_result.get("summary", ""), config.STATUS_NOT_APPLIED_YET, msg.date_utc),
            job_id=jid, contact_name=triage.get("contact_name", ""), fit_score=score,
        ))
        notifier.notify_new_match(company, title, score)
    sheet_rows.append({
        **dict.fromkeys(config.SHEET_COLUMNS, ""), "company": company, "job_id": jid,
        "status": config.STATUS_NOT_APPLIED_YET, "_row": row_number,
    })
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Mail Agent batch run")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions without writing")
    args = parser.parse_args()

    setup_logging()
    run(dry_run=args.dry_run)
