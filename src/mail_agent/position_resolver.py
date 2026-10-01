"""Makes a real effort to find a job posting's actual link + description + confirmed
company, instead of ever falling back to storing raw email content or guessed company
names in the sheet. Used both at mail-ingestion time and by the standalone backfill
script for historical gapped rows.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from . import classifier
from . import company_directory
from . import config
from . import job_page_fetcher
from .pipeline import dedup, reconcile, score
from .pipeline.types import Candidate, ReconcileResult, VerifiedPosition

logger = logging.getLogger(__name__)

MIN_CONTENT_LENGTH = 100

_URL_RE = re.compile(r"https?://\S+")
_SKIP_URL_MARKERS = ("unsubscribe", "mailto:", "tracking", "pixel", "optout", "opt-out")


@dataclass
class ResolvedPosition:
    url: str = ""
    description: str = ""
    company: str = ""


def _candidate_urls_from_body(email_body: str) -> list[str]:
    urls = []
    for match in _URL_RE.finditer(email_body or ""):
        url = match.group(0).rstrip(").,>\"'")
        low = url.lower()
        if any(marker in low for marker in _SKIP_URL_MARKERS):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def resolve_position(company: str, title: str, email_body: str = "") -> ResolvedPosition:
    """Never fabricates: url/description stay blank unless real fetched content was
    found. company stays as the original guess unless the LLM confirms otherwise
    against real page text."""
    url = ""
    description = ""

    for candidate in _candidate_urls_from_body(email_body):
        # LinkedIn serves public postings unauthenticated and needs its own
        # canonical-URL/retry handling (see job_page_fetcher.fetch_linkedin_posting).
        # Other ATSes vary -- Indeed in particular returns 401 to any direct fetch,
        # canonical URL or not, so a generic fetch is the most that's possible there.
        if "linkedin.com" in candidate.lower():
            posting = job_page_fetcher.fetch_linkedin_posting(candidate)
        else:
            posting = job_page_fetcher.fetch_generic_posting(candidate)
        if posting.description and len(posting.description) > MIN_CONTENT_LENGTH and not posting.closed:
            url = candidate
            description = posting.description[:2000]
            break

    if not url and config.ENABLE_CAREER_SITE_SEARCH:
        career_link, notes = company_directory.get_career_link(company)
        if career_link and "disqualified" not in (notes or "").lower():
            posting = job_page_fetcher.fetch_generic_posting(career_link)
            if posting.description:
                snippet = job_page_fetcher.extract_snippet_near(posting.description, title)
                if snippet:
                    url = career_link
                    description = snippet

    confirmed_company = company
    if description:
        confirmed_company = classifier.confirm_company_name(description, company)

    return ResolvedPosition(url=url, description=description, company=confirmed_company)


def resolve_from_page_text(
    company: str, title: str, url: str, page_text: str, sheet_rows: list[dict], sheets, dry_run: bool = False,
) -> ReconcileResult:
    """Resolve a position from text a HUMAN already retrieved by opening `url`
    themselves (e.g. a LinkedIn/Indeed posting this agent can't fetch automatically,
    read from an already-open Chrome tab) -- never a URL this process fetched itself.

    Deliberately reuses stage 4 (score.score_position) and stage 5
    (reconcile.reconcile) UNCHANGED rather than re-implementing scoring/writing: a
    manually-resolved posting is not a second-class path, it gets the exact same
    dedup, company-confirmation, and insert-vs-update handling a successful
    automated fetch would have produced. A synthetic Candidate/mail_id stands in for
    the (nonexistent, for this path) triggering email."""
    if len(page_text.strip()) < MIN_CONTENT_LENGTH:
        return ReconcileResult(action="low_fit", row_number=None, detail="page text too short to judge")

    confirmed_company = classifier.confirm_company_name(page_text, company)
    jid = dedup.job_id_for(confirmed_company, title)
    candidate = Candidate(
        mail_id=f"manual-chrome-{int(time.time())}", kind="opportunity", source="manual_browser",
        company=confirmed_company, title=title, url=url,
    )
    verified = VerifiedPosition(candidate=candidate, company=confirmed_company, title=title, url=url, description=page_text, job_id=jid)

    scored = score.score_position(verified)
    if scored is None:
        return ReconcileResult(action="low_fit", row_number=None, detail="below fit threshold, see skipped_candidates.csv")

    return reconcile.reconcile(scored, sheet_rows, sheets, dry_run)


_SIGNIFICANT_WORD_RE = re.compile(r"[a-zA-Z0-9]+")
_MIN_SIGNIFICANT_WORDS = 2


def _titles_match(wanted: str, candidate: str) -> bool:
    """Deliberately conservative, no fuzzy scoring: every significant word of `wanted`
    (order-independent, since a career site often appends a location/team suffix) must
    appear in `candidate`, and `wanted` must have at least 2 significant words -- never
    match on a single common word. This only ever runs against candidates
    job_page_fetcher.extract_job_links already validated as job-shaped links, which is
    strictly safer than config.ENABLE_CAREER_SITE_SEARCH's disabled raw-text match
    against a whole listing page (not re-enabled by this function)."""
    wanted_words = {w.lower() for w in _SIGNIFICANT_WORD_RE.findall(wanted) if len(w) > 1}
    if len(wanted_words) < _MIN_SIGNIFICANT_WORDS:
        return False
    candidate_words = {w.lower() for w in _SIGNIFICANT_WORD_RE.findall(candidate) if len(w) > 1}
    return wanted_words <= candidate_words


def find_on_career_page(company: str, title: str) -> ResolvedPosition:
    """Look up a specific position by title on the company's own (already-known or
    newly-discovered) career page -- the automatic alternative tried before ever
    asking you to open a link yourself. Returns an empty ResolvedPosition if the
    company has no usable career link, the page yields no job-shaped links (e.g.
    JS-rendered, same safe-failure as scan_career_pages.py), or none of them match
    the title -- never fabricates a match."""
    link, notes = company_directory.get_career_link(company)
    if not link or "disqualified" in (notes or "").lower():
        return ResolvedPosition()

    html = job_page_fetcher.fetch_raw_html(link)
    if not html:
        return ResolvedPosition()

    candidates = job_page_fetcher.extract_job_links(html, link)
    matched_url = next((url for cand_title, url in candidates if _titles_match(title, cand_title)), None)
    if not matched_url:
        return ResolvedPosition()

    posting = job_page_fetcher.fetch_generic_posting(matched_url)
    if not posting.description or posting.closed:
        return ResolvedPosition()

    confirmed_company = classifier.confirm_company_name(posting.description, company)
    return ResolvedPosition(url=matched_url, description=posting.description, company=confirmed_company)
