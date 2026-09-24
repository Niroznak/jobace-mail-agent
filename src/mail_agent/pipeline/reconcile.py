"""Stage 5: decide what a scored new position or a reply candidate actually means
for the sheet -- insert, update an existing row, flag as ambiguous, or drop -- and
write it. Every candidate regardless of source (digest, generic digest, single
email) funnels through the same functions here, which is the direct fix for the
class of bug where 5 near-duplicate write sites each got some detail slightly
wrong (blank in-memory title, a missing guardrail check, ...).
"""
from __future__ import annotations

import logging

from .. import config
from .. import guardrails
from .. import job_page_fetcher
from .. import notifier
from .. import position_sheet
from .. import sheets_client
from . import dedup
from .types import Candidate, ReconcileResult, ScoredItem

logger = logging.getLogger(__name__)


def reconcile(item: ScoredItem | Candidate, sheet_rows: list[dict], sheets, dry_run: bool) -> ReconcileResult:
    if isinstance(item, ScoredItem):
        return _reconcile_new_position(item, sheet_rows, sheets, dry_run)
    return _reconcile_reply(item, sheet_rows, sheets, dry_run)


def _reconcile_new_position(item: ScoredItem, sheet_rows: list[dict], sheets, dry_run: bool) -> ReconcileResult:
    v = item.verified
    c = v.candidate

    notes = sheets_client.append_status_history(item.summary, config.STATUS_NOT_APPLIED_YET, c.date_utc)
    record = position_sheet.PositionRecord(
        company=v.company, title=v.title, status=config.STATUS_NOT_APPLIED_YET, date_saved=c.date_utc,
        url=v.url, location=c.location, description=job_page_fetcher.focus_description(v.description, config.DESCRIPTION_STORE_CHARS),
        notes=notes, job_id=v.job_id, fit_score=item.score, contact_name=c.contact_name,
        requirements=item.requirements_json,
    )
    row_number = -1
    if not dry_run:
        row_number = position_sheet.append_position(sheets, record)
        notifier.notify_new_match(v.company, v.title, item.score)
    # Mirrors the real write (see PositionRecord) rather than a blank-title stub --
    # a stale in-memory title is exactly what let a second, distinct same-company
    # reply silently merge into a row later in the same run, before this existed.
    sheet_rows.append({**record.to_sheet_fields(), "_row": row_number})
    logger.info("[RECONCILE] '%s @ %s' score=%s -> inserted (row %s).", v.title, v.company, item.score, row_number)
    return ReconcileResult(action="inserted", row_number=row_number, detail=f"score={item.score}")


def _reconcile_reply(candidate: Candidate, sheet_rows: list[dict], sheets, dry_run: bool) -> ReconcileResult:
    if candidate.source == "linkedin_digest":
        # A digest-shaped body whose job_id is already tracked isn't a new
        # opportunity -- try the cheap job_id match first (this posting's own
        # listing ID), then fall back to the same company/title resolution a
        # single-email reply uses. Never creates a new row when unmatched --
        # allow_create_if_unmatched is False for this source (mirrors original:
        # a digest-shaped "status update" with no resolvable target is just
        # skipped, never promoted to a new row).
        jid = dedup.job_id_for(candidate.company, candidate.title, candidate.position_id)
        matched_row = sheets_client.find_row_by_job_id(sheet_rows, jid)
        match_tier = "job_id"
        if not matched_row:
            matched_row, ambiguous, match_tier = guardrails.resolve_reply_target_row(
                sheet_rows, candidate.company, candidate.title
            )
            if ambiguous:
                _flag_ambiguous(candidate, ambiguous)
                return ReconcileResult(action="ambiguous", row_number=None)
        if not matched_row:
            return ReconcileResult(action="dropped", row_number=None, detail="no matching row for digest status update")
        return _apply_status_signal(sheets, matched_row, candidate, dry_run, match_tier)

    # source == "single_email": no job_id pre-check in the original design --
    # goes straight to company/title resolution.
    matched_row, ambiguous, match_tier = guardrails.resolve_reply_target_row(sheet_rows, candidate.company, candidate.title)
    if ambiguous:
        _flag_ambiguous(candidate, ambiguous)
        return ReconcileResult(action="ambiguous", row_number=None)
    if matched_row:
        return _apply_status_signal(sheets, matched_row, candidate, dry_run, match_tier)
    if not candidate.allow_create_if_unmatched:
        return ReconcileResult(action="dropped", row_number=None, detail="no matching row, creation not allowed for this source")
    return _create_row_from_reply(candidate, sheet_rows, sheets, dry_run)


def _create_row_from_reply(candidate: Candidate, sheet_rows: list[dict], sheets, dry_run: bool) -> ReconcileResult:

    # No existing row for this company: write what we have rather than discard it
    # (a reply implies an application already happened, even if we never saw the
    # original opportunity email -- e.g. it was applied to directly on a career
    # site). Falls back to the email subject when the triage LLM couldn't extract
    # a role title from a terse confirmation body -- leaving this blank produced
    # an untitled, unidentifiable row (company only) that nothing could ever look
    # up or resolve later.
    title = candidate.title or candidate.subject or ""
    status = dedup.next_status("", candidate.status_signal) if candidate.status_signal else "applied"
    jid = dedup.job_id_for(candidate.company, title or candidate.company, candidate.position_id)
    if sheets_client.find_row_by_job_id(sheet_rows, jid):
        logger.info("[RECONCILE] application reply for '%s @ %s' already tracked, skipping.", title, candidate.company)
        return ReconcileResult(action="duplicate", row_number=None)

    logger.info("[RECONCILE] company=%s title=%r status=%s -> creating row from reply.", candidate.company, title, status)
    record = position_sheet.PositionRecord(
        company=candidate.company, title=title, status=status, date_saved=candidate.date_utc,
        notes=sheets_client.append_status_history(candidate.notes, status, candidate.date_utc),
        job_id=jid, contact_name=candidate.contact_name,
    )
    row_number = -1
    if not dry_run:
        row_number = position_sheet.append_position(sheets, record)
    sheet_rows.append({**record.to_sheet_fields(), "_row": row_number})
    return ReconcileResult(action="inserted", row_number=row_number, detail="created from reply, no prior row")


def _apply_status_signal(sheets, matched_row: dict, candidate: Candidate, dry_run: bool, match_tier: str) -> ReconcileResult:
    """`match_tier` is logged alongside the update purely for auditability -- when a
    wrong-row incident like the Mobileye/Maytronics ones happens again, the log
    should immediately say which tier justified the match instead of requiring a
    from-scratch reproduction to find out."""
    signal = candidate.status_signal
    if not signal:
        return ReconcileResult(action="dropped", row_number=matched_row.get("_row"), detail="no status signal in reply")

    old_status = matched_row.get("status", "")
    status = dedup.next_status(old_status, signal)
    logger.info(
        "[RECONCILE] %s -> status=%s (was %s, signal=%s, row %s, matched_via=%s)",
        candidate.company, status, old_status, signal, matched_row.get("_row"), match_tier,
    )
    fields = {"status": status}
    if candidate.contact_name:
        fields["contact_name"] = candidate.contact_name
    notes = matched_row.get("notes", "")
    if candidate.notes:
        notes = (notes + " | " + candidate.notes).strip(" |")
    # Record whenever real new information arrived: either the status itself
    # changed, or it's an "interview" signal -- next_status always returns
    # "interview" regardless of prior status, so a 2nd/3rd interview round would
    # otherwise look like a no-op and get silently dropped even though it's a
    # genuine new event worth a date. A repeated "applied"/"offer" ack with no
    # status change is the only case genuinely uninteresting to log.
    if status != old_status or signal == "interview":
        notes = sheets_client.append_status_history(notes, status, candidate.date_utc)
    fields["notes"] = notes

    if not dry_run:
        sheets_client.update_row_fields(sheets, matched_row["_row"], fields)
    # Keep the in-memory mirror accurate for the rest of this run -- the same
    # "stale in-memory copy" failure mode that let two distinct Mercor rejections
    # merge into one row applies here too if a later reply in this same run reads
    # this row's status/notes before they've been refreshed from the sheet.
    matched_row.update(fields)
    return ReconcileResult(action="updated", row_number=matched_row.get("_row"), detail=f"status={status}")


def _flag_ambiguous(candidate: Candidate, candidates: list[dict]) -> None:
    row_numbers = [c["_row"] for c in candidates]
    logger.warning(
        "[RECONCILE] [NEEDS REVIEW] '%s' status signal (title=%r, signal=%s) matches %d tracked rows for this "
        "company (rows %s) -- ambiguous, not auto-applying. Update manually.",
        candidate.company, candidate.title, candidate.status_signal, len(candidates), row_numbers,
    )
    notifier.notify_needs_review(
        f"Ambiguous status update for {candidate.company} ({candidate.status_signal}) -- "
        f"matches rows {row_numbers}, please update manually."
    )
