"""Fetches real job posting pages: LinkedIn's structured description markup, and a
generic best-effort fetch for any other URL (company career pages included).

LinkedIn serves job description HTML unauthenticated for public postings (verified
directly against a real posting). DuckDuckGo's plain HTML endpoint is used for
lightweight, no-API-key career-page discovery. Both are used only for postings/companies
you were personally sent or are actively tracking -- one request per lookup, with a real
browser User-Agent and a short pause between calls to stay a reasonable, low-volume
personal client rather than anything resembling scraping at scale.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# Primary marker matches LinkedIn's standard job-description container; a couple of
# fallback patterns cover minor markup variants seen across postings.
_DESCRIPTION_PATTERNS = [
    re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>\s*</div>', re.DOTALL),
    re.compile(r'class="description__text[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL),
    re.compile(r'<section class="core-section-container[^>]*description[^>]*>(.*?)</section>', re.DOTALL),
]
_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")
_FETCH_PACING_SECONDS = 2
_TIMEOUT_SECONDS = 20
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 4
_MIN_DESCRIPTION_LENGTH = 100  # guard against matching/accepting an empty/near-empty shell

_CLOSED_MARKERS = [
    "no longer accepting applications",
    "no longer accepting new applicants",
    "this job is no longer available",
    "position has been filled",
    "job posting has expired",
    "applications are closed",
    "this posting has expired",
]


@dataclass
class FetchedPosting:
    description: str = ""
    closed: bool = False


def is_closed_posting(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _CLOSED_MARKERS)


# A pre-scoring sanity gate: cheap, no LLM call, checked before any content is ever
# sent to cv_matcher.score_job_email. Real motivating cases: a JS-rendered page's
# bootstrap/theme JSON leaking through as "content" (jobs.nvidia.com/careers/job/...),
# and a fetched page that was real but simply wasn't a job posting (nav chrome +
# product blurb, ACS Motion Control) getting scored 65 against text that was never a
# job description at all. Neither the fetch layer nor the LLM's own score threshold
# caught these -- the LLM will confidently score whatever text it's handed, even text
# with no job-posting content in it. This is deliberately a low bar (any one signal
# word is enough): the goal is only to reject content that couldn't possibly be a real
# posting, not to second-guess borderline-but-real ones.
_JOB_POSTING_SIGNAL_WORDS = (
    "requirements", "qualifications", "responsibilities", "what you'll do",
    "what you will do", "we are looking for", "we're looking for", "about the role",
    "about this role", "job description", "years of experience", "nice to have",
    "must have", "preferred qualifications", "the ideal candidate", "you will",
)


_JSON_DUMP_MARKERS = ("&#34;", '\\"', '":', '": "')
# A real job description's prose essentially never contains these more than a
# handful of times; a JS-rendered page's embedded config/theme JSON (confirmed case:
# jobs.nvidia.com's Workday page dumps ~1 quote-marker every 13 chars) does, densely
# and consistently, even where a UI-label string coincidentally matches a signal word
# below (e.g. a Workday field literally labeled "Job Description" -- real bug: that
# label alone passed the word-based check and would have reached the LLM as if it
# were real content).
_JSON_DUMP_MAX_MARKERS_PER_1000_CHARS = 8


def _looks_like_json_dump(text: str) -> bool:
    if not text:
        return False
    marker_count = sum(text.count(m) for m in _JSON_DUMP_MARKERS)
    return (marker_count / len(text)) * 1000 > _JSON_DUMP_MAX_MARKERS_PER_1000_CHARS


def looks_like_job_posting(text: str) -> bool:
    low = (text or "").lower()
    if not any(word in low for word in _JOB_POSTING_SIGNAL_WORDS):
        return False
    return not _looks_like_json_dump(text)


_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def _clean_html(raw_html: str) -> str:
    # Strip script/style tag *bodies* first -- otherwise a modern JS-rendered career
    # page's embedded JSON/i18n-string state (real content, seen leaking through on
    # Similarweb's careers page) survives tag-stripping as if it were visible page
    # text, and can coincidentally contain a title's anchor word, producing a
    # false-positive "match" on pure UI/data noise instead of a real job description.
    html = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _TAG_RE.sub("\n", html)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_html(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        logger.warning("HTTP %s fetching %s", exc.code, url)
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Network error fetching %s: %s", url, exc)
        return None


def _extract_description(html: str) -> str:
    for pattern in _DESCRIPTION_PATTERNS:
        match = pattern.search(html)
        if match:
            text = _clean_html(match.group(1))
            if len(text) > _MIN_DESCRIPTION_LENGTH:
                return text
    return ""


def fetch_linkedin_posting(url: str) -> FetchedPosting:
    """Fetches a LinkedIn posting, extracting both its description and whether the
    page shows a closed/expired banner (checked over the *whole* cleaned page text,
    since that banner usually sits outside the description container). Retries and
    a fallback canonical URL are used before giving up -- this is content the
    recipient can see by clicking the link themselves, so a single failed attempt is
    not enough to give up."""
    # Try the canonical /jobs/view/<id>/ form first: the raw tracking URL LinkedIn
    # puts in alert emails (/comm/jobs/view/...?trackingId=...) reliably serves a
    # different template with no description marker at all (verified empirically),
    # so trying it first would just burn 3 guaranteed-failing attempts every time.
    job_id_match = _JOB_ID_RE.search(url)
    urls_to_try = []
    if job_id_match:
        urls_to_try.append(f"https://www.linkedin.com/jobs/view/{job_id_match.group(1)}/")
    if url not in urls_to_try:
        urls_to_try.append(url)

    for candidate_url in urls_to_try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            time.sleep(_FETCH_PACING_SECONDS)
            html = _fetch_html(candidate_url)
            if html:
                full_text = _clean_html(html)
                closed = is_closed_posting(full_text)
                description = _extract_description(html)
                if closed or description:
                    return FetchedPosting(description=description, closed=closed)
                logger.warning(
                    "Fetched %s (attempt %d/%d) but no description marker found (page length %d)",
                    candidate_url, attempt, _MAX_ATTEMPTS, len(html),
                )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)

    logger.error("Exhausted all attempts and URL forms for job posting: %s", url)
    return FetchedPosting()


def fetch_linkedin_description(url: str) -> str:
    """Backward-compatible thin wrapper returning just the description text."""
    return fetch_linkedin_posting(url).description


def fetch_raw_html(url: str) -> str | None:
    """Public wrapper around the internal fetch used by scan_career_pages.py, which
    needs the raw HTML (to find links via extract_job_links) rather than the cleaned
    plain text fetch_generic_posting returns."""
    return _fetch_html(url)


_ANCHOR_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_JOB_LINK_PATH_MARKERS = (
    "job", "jobs", "position", "vacancy", "career", "req", "posting",
    # Known ATS platforms companies embed job listings from under their own domain
    # (e.g. acsmotioncontrol.com/comeet/...) -- a real, reliable job-link signal even
    # when the URL's own path has no job-related word in it at all (Comeet's own path
    # scheme is a numeric position code, not a keyword).
    "comeet", "greenhouse", "lever", "workday", "ashby", "smartrecruiters",
    "recruitee", "bamboohr", "icims", "jazzhr", "breezy", "workable",
)
_NAV_NOISE_WORDS = {
    "home", "about", "about us", "contact", "contact us", "login", "log in", "sign in",
    "search", "filter", "filters", "reset", "next", "previous", "back", "menu",
    "privacy policy", "terms", "cookie", "cookies", "faq", "careers", "all jobs",
}
# Generic call-to-action link text used across many career-page templates for a
# card's "go to detail page" link, with the real title living in a separate heading
# element the anchor-only regex can't see. Real bug: "Read More >" (HTML-entity
# undecoded) was accepted as a "job title" at Camtek, once per job card on the page,
# and each got individually LLM-scored (wildly inconsistent scores, 20-72, since
# there's no real title/content signal to score) before dry-run caught it pre-write.
# Checked as a substring, not exact match, since real anchors include trailing/leading
# whitespace, arrows, or icons around these phrases.
_CTA_NOISE_PHRASES = (
    "read more", "learn more", "view more", "view job", "view details", "view all",
    "view position", "see more", "see details", "click here", "apply now", "apply here",
    "more info", "more information", "find out more", "details", "search jobs",
    "skip to", "skip navigation", "show more", "load more",
)
_MIN_TITLE_LEN = 10
_MAX_TITLE_LEN = 100
_NAV_CHROME_RE = re.compile(r"(?is)<(nav|header|footer)\b[^>]*>.*?</\1>")


def extract_job_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Best-effort, generic extraction of (title, url) pairs for individual job
    postings from a company's career/listing page. No site-specific scraping -- every
    company's markup differs, so this is a heuristic (same-domain link + job-shaped
    URL path + plausible title text), not a guarantee. Expected to return [] for
    JS-rendered listing pages (confirmed case: career.rafael.co.il serves an empty
    bot-protection shell to a plain fetch) -- that's a safe failure mode, not a bug:
    zero candidates just means nothing new is found this run, never a wrong one.
    Same principle applies to card layouts using a generic "Read More"-style link
    with the real title in a separate element this anchor-only heuristic can't reach
    -- those cards correctly yield no candidate rather than a wrong one.

    Real bug: a career page's shared site-wide <nav>/<header>/<footer> (present on
    every page of the domain, not just the careers page) contained an ordinary
    "Products" menu item ("Intelligent Drive Modules") that passed every other check
    -- same domain, real page, plausible-length title -- and got written to the sheet
    as a fabricated job (ACS Motion Control, 2026-09-18). Stripped before anchor
    extraction even looks at the page, the same way _clean_html strips script/style
    bodies first."""
    html = _NAV_CHROME_RE.sub(" ", html)
    base_domain = urllib.parse.urlparse(base_url).netloc.lower()
    seen_urls: set[str] = set()
    results: list[tuple[str, str]] = []
    for match in _ANCHOR_RE.finditer(html):
        href, inner_html = match.group(1), match.group(2)
        title = _TAG_RE.sub(" ", inner_html)
        title = html_module.unescape(title)
        title = re.sub(r"\s+", " ", title).strip()
        if not (_MIN_TITLE_LEN <= len(title) <= _MAX_TITLE_LEN):
            continue
        title_lower = title.lower()
        if title_lower in _NAV_NOISE_WORDS:
            continue
        if any(phrase in title_lower for phrase in _CTA_NOISE_PHRASES):
            continue

        absolute_url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(absolute_url)
        if parsed.netloc.lower() != base_domain:
            continue
        path_lower = parsed.path.lower()
        # Real bug: a bare "3+ digits anywhere in the path" fallback (since removed)
        # matched "402" inside a product page's model-number slug
        # ("/products/ds402-ethercat-servo-drives/"), which then got scored and
        # written to the sheet as a fabricated "job" (ACS Motion Control, 2026-09-18).
        # A keyword/ATS-marker match is required now -- no bare-digit fallback.
        if not any(marker in path_lower for marker in _JOB_LINK_PATH_MARKERS):
            continue

        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        results.append((title, absolute_url))
    return results


def fetch_generic_posting(url: str) -> FetchedPosting:
    """Best-effort fetch for any non-LinkedIn URL (company career pages included).
    No structured extraction is attempted -- `description` is the whole cleaned page
    text; callers decide whether to store it as-is or pull a snippet out of it via
    `extract_snippet_near`. Returns an empty/False result on fetch failure."""
    html = _fetch_html(url)
    if not html:
        return FetchedPosting()
    text = _clean_html(html)
    return FetchedPosting(description=text, closed=is_closed_posting(text))


_RESULT_LINK_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"')


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


def search_google_custom_full(query: str, max_results: int = 3) -> list[SearchResult]:
    """Career-page discovery via Google Programmable Search's Custom Search JSON API
    (free tier: 100 queries/day). Unlike DuckDuckGo's plain-HTML endpoint, this is a
    real API with a key -- no CAPTCHA-blocking, and it can find JS-rendered pages
    since Google's own crawler already indexed their rendered content (a plain fetch
    of the URL itself may still come back empty for those -- callers can fall back to
    the title/snippet here as a weaker-but-real verification signal in that case).
    Returns [] (never raises) if the key/cx aren't configured or the request fails, so
    callers can fall back or treat it as "search unavailable" -- never invents a result."""
    if not config.GOOGLE_SEARCH_API_KEY or not config.GOOGLE_SEARCH_CX:
        return []
    params = {
        "key": config.GOOGLE_SEARCH_API_KEY,
        "cx": config.GOOGLE_SEARCH_CX,
        "q": query,
        "num": min(max_results, 10),
    }
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.exception("Google Custom Search request failed for query=%r", query)
        return []
    items = data.get("items", [])
    return [
        SearchResult(url=item["link"], title=item.get("title", ""), snippet=item.get("snippet", ""))
        for item in items if item.get("link")
    ][:max_results]


def search_google_custom(query: str, max_results: int = 3) -> list[str]:
    return [r.url for r in search_google_custom_full(query, max_results)]


def search_serpapi_full(query: str, max_results: int = 3) -> list[SearchResult]:
    """Career-page discovery via SerpAPI (real Google results, no CAPTCHA, free tier:
    100 searches/month). Used in place of Google's own Custom Search API because
    "Search the entire web" was locked/grayed out on the account this was set up
    with. Returns [] (never raises) if the key isn't configured or the request
    fails -- never invents a result."""
    if not config.SERPAPI_API_KEY:
        return []
    params = {
        "engine": "google",
        "q": query,
        "api_key": config.SERPAPI_API_KEY,
        "num": min(max_results, 10),
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.exception("SerpAPI request failed for query=%r", query)
        return []
    items = data.get("organic_results", [])
    return [
        SearchResult(url=item["link"], title=item.get("title", ""), snippet=item.get("snippet", ""))
        for item in items if item.get("link")
    ][:max_results]


def search_duckduckgo(query: str, max_results: int = 3) -> list[str]:
    """Lightweight, no-API-key web search via DuckDuckGo's plain HTML endpoint.
    Best-effort: DDG's markup can change, so this is not guaranteed to keep working,
    but it needs no account/key for a low-volume personal-use lookup."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    time.sleep(_FETCH_PACING_SECONDS)
    html = _fetch_html(url)
    if not html:
        return []
    results = []
    for match in _RESULT_LINK_RE.finditer(html):
        href = match.group(1)
        # DDG wraps real URLs in a redirect: /l/?uddg=<url-encoded-real-url>&...
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        real_url = qs.get("uddg", [href])[0]
        real_url = urllib.parse.unquote(real_url)
        if real_url not in results:
            results.append(real_url)
        if len(results) >= max_results:
            break
    return results


def extract_snippet_near(page_text: str, title: str, window: int = 800) -> str:
    """Returns `window` chars of context around the first place `title`'s significant
    words appear together in `page_text`, or "" if the title isn't found there. Keeps
    a company's whole careers-listing page from ever being stored wholesale as a
    single position's description.

    Requires the full title phrase (all significant words, in order, loosely
    separated) rather than a single anchor word -- a career page's own nav/marketing
    chrome can coincidentally contain one common title word in isolation (e.g. a
    "Research & Analysts" menu item matching a "...Analyst" title) even after
    script/style content is stripped, producing a false-positive match on page
    furniture instead of a real job listing."""
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", (title or "").lower()) if len(w) > 1]
    if len(words) < 2:
        return ""
    pattern = r"\b" + r"[\s&/,-]+".join(re.escape(w) for w in words) + r"\b"
    match = re.search(pattern, page_text, re.IGNORECASE)
    if not match:
        return ""
    start = max(0, match.start() - window // 2)
    end = min(len(page_text), match.end() + window // 2)
    return page_text[start:end].strip()
