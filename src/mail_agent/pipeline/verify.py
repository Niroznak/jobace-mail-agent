"""Stage 3: confirm a Candidate(kind="opportunity") is a real, live, grounded
position -- not a duplicate, not closed, not junior/out-of-range/a generic listing
label, and backed by a real fetched description. Only ever called for
kind="opportunity" candidates; reply candidates skip straight to stage 5.

Retry semantics for "couldn't verify yet" (no real description found): the ORIGINAL
email still gets marked read this run (see scripts/main.py) -- Gmail will never
re-serve it, so retrying by re-fetching the same email isn't an option. Instead the
full Candidate is persisted to a small local JSON (state.load/save_pending_verification,
keyed by job_id), and scripts/main.py re-feeds every pending candidate back through
this same function on each future run via `pending_candidates()`, independent of
any message that run. If verification still fails after
config.MAX_RESOLUTION_ATTEMPTS, it's dropped entirely (never written, not even as a
placeholder) rather than left as a dead "nr" row -- unlike a reply-derived row
(which represents a real event that happened even without a link), a freshly-
discovered opportunity that never verifies as real carries no such guarantee.
"""
from __future__ import annotations

import dataclasses
import logging

from .. import classifier
from .. import config
from .. import guardrails
from .. import job_page_fetcher
from .. import position_resolver
from .. import sheets_client
from . import dedup
from .types import Candidate, VerifiedPosition

logger = logging.getLogger(__name__)


def _passes_early_filters(candidate: Candidate) -> bool:
    """The four checks every opportunity candidate must pass regardless of source,
    applied uniformly here instead of duplicated per-source (that duplication is
    exactly how a nav-link "Open Positions" title reached one source but not
    another, this session)."""
    company, title = candidate.company, candidate.title
    if classifier.is_platform_company_name(company):
        return False
    if guardrails.looks_like_generic_listing_title(title):
        logger.info("[VERIFY] '%s @ %s' -> generic listing label, not a specific position.", title, company)
        return False
    if classifier.is_junior_or_intern_title(title):
        logger.info("[VERIFY] '%s @ %s' -> junior/intern/student title, not relevant.", title, company)
        return False
    if classifier.is_location_excluded(candidate.location):
        logger.info("[VERIFY] '%s @ %s' -> location '%s' not commutable, not relevant.", title, company, candidate.location)
        return False
    return True


def pending_candidates(retry_state: dict) -> list[Candidate]:
    """Reconstructs every not-yet-given-up-on candidate from persisted state, so
    scripts/main.py can re-feed them through verify_position on this run alongside
    freshly extracted ones -- this is the actual retry mechanism, since the
    original triggering email is marked read and never re-served by Gmail."""
    return [Candidate(**entry["candidate"]) for entry in retry_state.values()]


def _give_up_or_retry(job_id: str, candidate: Candidate, retry_state: dict) -> bool:
    """Records a failed verification attempt for `job_id`, persisting the full
    candidate. Returns True if attempts are now exhausted (caller should drop this
    candidate entirely -- the entry is removed from retry_state either way in that
    case)."""
    entry = retry_state.get(job_id) or {"attempts": 0, "candidate": dataclasses.asdict(candidate)}
    entry["attempts"] += 1
    if entry["attempts"] >= config.MAX_RESOLUTION_ATTEMPTS:
        retry_state.pop(job_id, None)
        return True
    retry_state[job_id] = entry
    return False


def verify_position(candidate: Candidate, sheet_rows: list[dict], retry_state: dict) -> VerifiedPosition | None:
    if not _passes_early_filters(candidate):
        return None
    if candidate.source == "linkedin_digest":
        return _verify_linkedin_digest(candidate, sheet_rows, retry_state)
    if candidate.source == "generic_digest":
        return _verify_generic_digest(candidate, sheet_rows)
    return _verify_single_email(candidate, sheet_rows, retry_state)


def _verify_linkedin_digest(candidate: Candidate, sheet_rows: list[dict], retry_state: dict[str, int]) -> VerifiedPosition | None:
    company, title = candidate.company, candidate.title
    # Stage 1 dedup: cheap check by LinkedIn's own listing ID, before spending a
    # fetch. Only checked against active rows -- a closed row's job_id must not
    # block a fresh listing (e.g. the same requisition reopened) from being added
    # as new.
    pid_jid = dedup.job_id_for(company, title, candidate.position_id)
    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), pid_jid):
        logger.info("[VERIFY] '%s @ %s' already tracked (listing ID), skipping.", title, company)
        return None

    fetched = job_page_fetcher.fetch_linkedin_posting(candidate.url) if candidate.url else job_page_fetcher.FetchedPosting()
    if fetched.closed:
        logger.info("[VERIFY] '%s @ %s' -> posting no longer accepting applications, skipping.", title, company)
        return None
    description = fetched.description
    if not description:
        if _give_up_or_retry(pid_jid, candidate, retry_state):
            logger.info("[VERIFY] '%s @ %s' -> giving up after %d attempts, dropping.", title, company, config.MAX_RESOLUTION_ATTEMPTS)
        else:
            logger.info("[VERIFY] '%s @ %s' -> could not fetch job description, will retry.", title, company)
        return None

    # Stage 2 dedup: stronger check by the employer's own requisition ID, now that
    # we have the real description -- catches a role that closed and reopened
    # under a new LinkedIn listing ID (same requisition, different pid_jid above).
    requisition_id = classifier.extract_requisition_id(description)
    jid = dedup.job_id_for(company, title, candidate.position_id, requisition_id)
    if requisition_id and sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[VERIFY] '%s @ %s' already tracked (requisition ID %s), skipping.", title, company, requisition_id)
        return None

    # Stage 3 dedup: exact-description match -- ONLY when no requisition ID was
    # stated at all (a real requisition ID always trumps description matching; see
    # dedup.job_id_for's own docstring for why).
    if not requisition_id and sheets_client.find_row_by_company_and_description(
        sheets_client.active_rows(sheet_rows), company, description[:config.DESCRIPTION_STORE_CHARS]
    ):
        logger.info("[VERIFY] '%s @ %s' already tracked (identical description, no requisition ID stated), skipping.", title, company)
        return None

    company, jid = _confirm_company(company, title, description, candidate, jid)
    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[VERIFY] '%s @ %s' already tracked under confirmed company, skipping.", title, company)
        return None

    retry_state.pop(pid_jid, None)
    return VerifiedPosition(candidate=candidate, company=company, title=title, url=candidate.url,
                             description=description, job_id=jid, requisition_id=requisition_id)


def _confirm_company(company: str, title: str, description: str, candidate: Candidate, jid: str) -> tuple[str, str]:
    """Verifies the hiring company against the real fetched page text, recomputing
    the dedup key if it changed. Real incident: a LinkedIn digest listed "CargoSeer"
    as the company, but the actual posting page explicitly said "...interviewing at
    BigBear.ai" -- CargoSeer didn't appear anywhere on the real page at all. The
    single-email path already had this via position_resolver.resolve_position; the
    digest paths (LinkedIn and generic) never verified the digest-extracted company
    name against anything, permanently trusting whatever LinkedIn's email text said."""
    confirmed = classifier.confirm_company_name(description, company)
    if confirmed and confirmed != company:
        logger.info("[VERIFY] '%s' -> confirmed as '%s' for '%s'.", company, confirmed, title)
        jid = dedup.job_id_for(confirmed, title, candidate.position_id)
        return confirmed, jid
    return company, jid


# Real incident: an Indeed alert's ~160-char teaser ("Experience... role, you will...")
# passed the old 100-char bar and was scored 73 for a chip-design role the candidate
# has no background for -- the teaser hides the requirements. Indeed blocks fetching
# the full page (HTTP 403), so a teaser can never justify a fit score.
MIN_DIGEST_DESCRIPTION_CHARS = 500


def _verify_generic_digest(candidate: Candidate, sheet_rows: list[dict]) -> VerifiedPosition | None:
    company, title = candidate.company, candidate.title
    jid = dedup.job_id_for(company, title)
    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[VERIFY] '%s @ %s' already tracked, skipping.", title, company)
        return None

    description = candidate.snippet.strip()
    if len(description) < MIN_DIGEST_DESCRIPTION_CHARS and candidate.url:
        try:
            fetched = job_page_fetcher.fetch_generic_posting(candidate.url)
        except Exception:
            fetched = job_page_fetcher.FetchedPosting()
        if fetched.description and not fetched.closed:
            description = fetched.description

    if len(description) < MIN_DIGEST_DESCRIPTION_CHARS:
        # Never score/decide fitness without real content -- a digest snippet this
        # short (or a fetch that failed, e.g. Indeed's known 401 block) isn't an
        # honest basis for a match/no-match judgment. No retry tracking here (same
        # as before this refactor) -- the source email is a one-shot digest, not
        # something backfill_career_links.py-style logic ever revisited.
        logger.info(
            "[VERIFY] '%s @ %s' -> no real description available (digest snippet too short, fetch failed), not scoring.",
            title, company,
        )
        return None

    company, jid = _confirm_company(company, title, description, candidate, jid)
    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[VERIFY] '%s @ %s' already tracked under confirmed company, skipping.", title, company)
        return None

    return VerifiedPosition(candidate=candidate, company=company, title=title, url=candidate.url,
                             description=description, job_id=jid)


def _verify_single_email(candidate: Candidate, sheet_rows: list[dict], retry_state: dict[str, int]) -> VerifiedPosition | None:
    company, title, content = candidate.company, candidate.title, candidate.snippet
    jid = dedup.job_id_for(company, title, candidate.position_id)

    if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
        logger.info("[VERIFY] '%s @ %s' already tracked, skipping.", title, company)
        return None

    # Re-key on the employer's own requisition ID if this email's raw content
    # happens to state one (same stability rationale as the digest path) --
    # extracted from the RAW EMAIL content here, deliberately before ever fetching
    # the real posting page (mirrors original behavior exactly).
    requisition_id = classifier.extract_requisition_id(content)
    if requisition_id:
        jid = dedup.job_id_for(company, title, candidate.position_id, requisition_id)
        if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
            logger.info("[VERIFY] '%s @ %s' already tracked (requisition ID %s), skipping.", title, company, requisition_id)
            return None

    # Make a real effort to get the actual posting link/description/company rather
    # than ever storing the raw email body as "description" -- see position_resolver.
    resolved = position_resolver.resolve_position(company, title, content)
    if not resolved.description:
        if _give_up_or_retry(jid, candidate, retry_state):
            logger.info("[VERIFY] '%s @ %s' -> giving up after %d attempts, dropping.", title, company, config.MAX_RESOLUTION_ATTEMPTS)
        else:
            logger.info("[VERIFY] '%s @ %s' -> could not verify a real posting description, will retry.", title, company)
        return None

    if resolved.company != company:
        logger.info("[VERIFY] '%s' -> confirmed as '%s' for '%s'.", company, resolved.company, title)
        company = resolved.company
        jid = dedup.job_id_for(company, title, candidate.position_id, requisition_id)
        if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
            logger.info("[VERIFY] '%s @ %s' already tracked under confirmed company, skipping.", title, company)
            return None

    retry_state.pop(jid, None)
    return VerifiedPosition(candidate=candidate, company=company, title=title, url=resolved.url,
                             description=resolved.description, job_id=jid, requisition_id=requisition_id)
