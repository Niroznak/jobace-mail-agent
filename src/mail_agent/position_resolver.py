"""Makes a real effort to find a job posting's actual link + description + confirmed
company, instead of ever falling back to storing raw email content or guessed company
names in the sheet. Used both at mail-ingestion time and by the standalone backfill
script for historical gapped rows.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import classifier
from . import company_directory
from . import config
from . import job_page_fetcher

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
