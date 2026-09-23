"""Stage 2: turn one fetched email into a list of Candidate objects -- purely
structural extraction (what does this email say, and is it about something
already tracked). No fetching real posting content, no scoring, no writing --
those are stages 3/4/5. Needs `sheet_rows` (an in-memory list, not network I/O) to
decide whether a digest-shaped posting is a new opportunity or a status update
about something already tracked; that's the one piece of sheet state this stage
can't avoid needing.
"""
from __future__ import annotations

import logging

from .. import classifier
from .. import sheets_client
from ..gmail_client import EmailMessage
from . import dedup
from .types import Candidate

logger = logging.getLogger(__name__)


def extract_candidates(msg: EmailMessage, sheet_rows: list[dict]) -> list[Candidate]:
    if classifier.is_connection_request(msg):
        logger.info("[EXTRACT] mail=%s -> LinkedIn connection request, not a job opportunity.", msg.id)
        return []

    digest_postings = classifier.parse_linkedin_digest(msg.body)
    if digest_postings:
        candidates = _extract_from_linkedin_digest(msg, digest_postings, sheet_rows)
        logger.info("[EXTRACT] mail=%s -> %d candidate(s) (linkedin digest).", msg.id, len(candidates))
        return candidates

    return _extract_from_single_email(msg, sheet_rows)


def _extract_from_linkedin_digest(msg: EmailMessage, digest_postings: list[dict], sheet_rows: list[dict]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for p in digest_postings:
        pid_jid = dedup.job_id_for(p["company"], p["title"], p["position_id"])
        already_tracked = sheets_client.find_row_by_job_id(sheet_rows, pid_jid)
        # Guard against "you might also like" recommendations appended below a real
        # single-posting notification (e.g. a LinkedIn "application sent" email) --
        # those share the same digest body shape and can incidentally match an
        # already-tracked job_id, but the confirmation/status content is about the
        # company actually named in the subject, not these unrelated extras. Only
        # treat this as a status update when the subject itself names this
        # posting's company.
        is_subject_target = p["company"].strip().lower() in msg.subject.lower()
        if already_tracked and is_subject_target:
            triage = classifier.classify_email(msg)
            if triage.get("category") != "application_reply" or not triage.get("status"):
                continue
            candidates.append(Candidate(
                mail_id=msg.id, kind="reply", source="linkedin_digest",
                company=p["company"] or triage.get("company", ""), title=p["title"],
                position_id=p["position_id"], status_signal=triage.get("status", ""),
                notes=triage.get("notes", ""), contact_name=triage.get("contact_name", ""),
                date_utc=msg.date_utc, allow_create_if_unmatched=False,
            ))
        else:
            candidates.append(Candidate(
                mail_id=msg.id, kind="opportunity", source="linkedin_digest",
                company=p["company"], title=p["title"], location=p.get("location", ""),
                url=p.get("url", ""), position_id=p.get("position_id", ""), date_utc=msg.date_utc,
            ))
    return candidates


def _extract_from_single_email(msg: EmailMessage, sheet_rows: list[dict]) -> list[Candidate]:
    triage = classifier.classify_email(msg)
    category = triage.get("category", "other")
    company = triage.get("company", "")

    if category == "application_reply" and company and not classifier.is_platform_company_name(company):
        candidates = [Candidate(
            mail_id=msg.id, kind="reply", source="single_email",
            company=company, title=triage.get("role_title", "") or "", subject=msg.subject,
            position_id=triage.get("position_id", ""), status_signal=triage.get("status", ""),
            notes=triage.get("notes", ""), contact_name=triage.get("contact_name", ""),
            date_utc=msg.date_utc, allow_create_if_unmatched=True,
        )]
        logger.info("[EXTRACT] mail=%s -> 1 candidate (application reply, company=%s).", msg.id, company)
        return candidates

    looks_like_unresolved_digest = category != "job_opportunity" or (
        not company and classifier.is_platform_domain(msg.sender_email)
    )
    if looks_like_unresolved_digest:
        # Before giving up: the single-opportunity triage prompt can't extract "the"
        # company from an email that genuinely bundles several (an Indeed/Glassdoor
        # "N new jobs" alert, not just LinkedIn's own digest format) -- try the
        # generic multi-posting extraction as a recovery path rather than silently
        # dropping real postings. Only acts if it confidently finds >=2 distinct
        # real postings; otherwise this is a no-op and the skip logic below runs.
        generic_postings = classifier.parse_generic_digest(msg.subject, msg.body)
        if generic_postings:
            candidates = [
                Candidate(
                    mail_id=msg.id, kind="opportunity", source="generic_digest",
                    company=p["company"], title=p["title"], location=p.get("location", ""),
                    url=p.get("url", ""), snippet=p.get("snippet", ""), date_utc=msg.date_utc,
                )
                for p in generic_postings
            ]
            logger.info("[EXTRACT] mail=%s -> %d candidate(s) (generic digest recovery).", msg.id, len(candidates))
            return candidates

    if category != "job_opportunity":
        logger.info("[EXTRACT] mail=%s -> not a job opportunity or known application (category=%s).", msg.id, category)
        return []

    if classifier.is_platform_company_name(company) or (not company and classifier.is_platform_domain(msg.sender_email)):
        logger.info("[EXTRACT] mail=%s -> job opportunity but no identifiable hiring company (got %r).", msg.id, company)
        return []

    company = company or msg.sender_name or msg.sender_email
    title = triage.get("role_title") or msg.subject
    content = msg.body or msg.snippet
    position_id = classifier.extract_linkedin_job_id(content) or triage.get("position_id", "")
    candidates = [Candidate(
        mail_id=msg.id, kind="opportunity", source="single_email",
        company=company, title=title, subject=msg.subject, location=triage.get("location", ""),
        snippet=content, position_id=position_id, contact_name=triage.get("contact_name", ""),
        date_utc=msg.date_utc,
    )]
    logger.info("[EXTRACT] mail=%s -> 1 candidate (single opportunity, company=%s).", msg.id, company)
    return candidates
