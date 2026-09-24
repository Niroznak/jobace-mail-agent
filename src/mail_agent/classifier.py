"""Email triage: company-reply vs job-opportunity classification.

Promotional mail is filtered entirely by the Gmail Apps Script (moveCommercialsToSpam) --
this agent only scans the "Work" label, which promotional mail never gets tagged into.
"""
from __future__ import annotations

import logging
import re

from . import llm_client
from . import config
from . import guardrails
from . import notifier
from .gmail_client import EmailMessage

logger = logging.getLogger(__name__)


# --- LinkedIn digest parsing ---
# LinkedIn "N new jobs match your preferences" emails bundle multiple distinct
# postings in one message, each block formatted deterministically as:
#   <Title>\n<Company>\n<Location>\n[badge line]\nApply with resume & profile\nView job: <url>
# separated by a line of dashes. Parsing this directly (regex, no LLM) is far more
# reliable than asking the single-posting LLM schema to enumerate multiple jobs --
# it also recovers the real posting URL and a real LinkedIn job ID for dedup, neither
# of which the LLM triage can see reliably in truncated plain text.
_VIEW_JOB_RE = re.compile(r"View job:\s*(https?://\S+)")
_JOB_ID_IN_URL_RE = re.compile(r"/jobs/view/(\d+)")
_DIGEST_BADGE_LINES = {
    "top applicant", "fast growing", "this company is actively hiring",
    "apply with resume & profile",
}
_SEPARATOR_RE = re.compile(r"^-{5,}$")
_ALERT_PREAMBLE_RE = re.compile(r"^(?:\d+\s+)?new jobs? match your preferences\.?$", re.IGNORECASE)

# LinkedIn sometimes bakes a trailing work-mode/location tag directly into the title
# line itself (e.g. "AI Engineer - Technical Enablement - remote/Tel-Aviv"), even when
# the actual location is separately and correctly captured on its own line below (e.g.
# "Hadera"). Left in place, this noise breaks title-based matching against an ATS
# confirmation email's clean title for the same role -- narrowly scoped to a trailing
# remote/hybrid/onsite tag (optionally with a "/City" suffix) so it never strips a
# real title suffix like "- Individual Contributor".
_TITLE_WORKMODE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(remote|hybrid|onsite|on-site)(\s*/\s*[A-Za-z][A-Za-z\s.'-]*)?\s*$",
    re.IGNORECASE,
)


def strip_workmode_suffix(title: str) -> str:
    return _TITLE_WORKMODE_SUFFIX_RE.sub("", title).strip()


def parse_linkedin_digest(body: str) -> list[dict]:
    """Returns [{"title", "company", "location", "url", "position_id"}, ...] for each
    real job card found, or [] if none.

    Anchors on each "View job: <url>" line and walks backward up to 3 non-blank lines
    to recover title/company/location, stopping at any boundary marker (a separator
    line, a bare URL, or the alert-digest preamble). This is deliberately anchored on
    the URL line rather than splitting the whole body into blocks -- some LinkedIn
    "recommendations" digests interleave a category header + bare search-results URL
    directly above a job card with no separator, which broke block-based splitting
    (it picked up "Expand your search" / "Recommendations based on your activity." as
    the title/company). Anchoring on the URL and stopping once 3 lines are collected
    avoids ever reaching that unrelated preamble text.
    """
    lines = body.splitlines()
    postings = []
    for i, line in enumerate(lines):
        match = _VIEW_JOB_RE.search(line)
        if not match:
            continue
        url = match.group(1)
        collected: list[str] = []
        j = i - 1
        while j >= 0 and len(collected) < 3:
            raw = lines[j].strip()
            j -= 1
            if not raw:
                continue
            low = raw.lower()
            if low in _DIGEST_BADGE_LINES or re.match(r"^\d+\s+(connections?|school alum(?:ni)?|mutual connections?)$", low):
                continue
            if _SEPARATOR_RE.match(raw) or low.startswith("http://") or low.startswith("https://"):
                break
            if low.startswith("your job alert for") or _ALERT_PREAMBLE_RE.match(raw):
                break
            collected.append(raw)
        collected.reverse()
        if len(collected) < 2:
            continue
        # LinkedIn injects invisible/narrow unicode spacing (anti-scraping artifact)
        # between words in some titles -- normalize to regular spaces.
        collected = [re.sub(r"\s+", " ", c).strip() for c in collected]
        title, company = strip_workmode_suffix(collected[0]), collected[1]
        location = collected[2] if len(collected) >= 3 else ""
        job_id_match = _JOB_ID_IN_URL_RE.search(url)
        postings.append({
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "position_id": job_id_match.group(1) if job_id_match else "",
        })
    return postings


# --- Generic multi-position digest parsing (Indeed, Glassdoor, and any other job-alert
# source not worth a bespoke regex parser like LinkedIn's above) ---
# Unlike parse_linkedin_digest, this is LLM-based rather than regex-anchored: every
# other digest source has its own markup shape, and hand-writing a new regex parser
# per sender doesn't scale (see tasks/lessons.md -- "expect more format variants, not
# fewer"). The LLM is instead asked to extract every distinct real posting mentioned,
# using only text literally present in the email -- never invents a posting, and is
# explicitly told to return nothing if it can't confidently find at least 2 distinct
# ones, so a normal single-opportunity email is never misrouted here.
_GENERIC_DIGEST_PROMPT = """\
This email may be a job-alert digest bundling MULTIPLE distinct job postings from \
different companies (e.g. an Indeed or Glassdoor "N new jobs" alert), rather than a \
single opportunity. Read it and answer with ONLY valid JSON, no markdown fences:
{{
  "postings": [
    {{
      "title": "<job title, exactly as stated>",
      "company": "<hiring company name, exactly as stated>",
      "location": "<city/region if stated, else empty string>",
      "url": "<the direct link to this specific posting, if one is present near it, else empty string>",
      "snippet": "<the real description/snippet text for this posting as it appears in the email, verbatim -- empty string if none>"
    }}
  ]
}}

Rules:
- Only include postings you can point to a specific title+company for, literally present in the text.
- NEVER invent a posting, a title, a company, or a snippet -- copy only what the email actually says.
- If this email does not contain at least 2 distinct real postings (e.g. it's about a single company,
  or you can't confidently separate the postings), return {{"postings": []}}.

EMAIL:
Subject: {subject}
Body:
{body}
"""


def parse_generic_digest(subject: str, body: str) -> list[dict]:
    """Returns [{"title", "company", "location", "url", "snippet"}, ...] for each
    distinct posting the LLM could confidently identify, or [] if it found fewer than
    2 (callers should fall back to normal single-email handling in that case -- this
    function is only a recovery path for genuine multi-company digests, not a
    replacement for the single-opportunity triage prompt)."""
    prompt = _GENERIC_DIGEST_PROMPT.format(subject=subject, body=(body or "")[:4000])
    try:
        result = llm_client.call_json(prompt, num_predict=1000)  # a digest can list several postings
    except Exception:
        logger.exception("Generic digest parsing failed")
        return []
    postings = result.get("postings", [])
    if not isinstance(postings, list) or len(postings) < 2:
        return []
    cleaned = []
    for p in postings:
        title, company = (p.get("title") or "").strip(), (p.get("company") or "").strip()
        if not title or not company:
            continue
        cleaned.append({
            "title": title,
            "company": company,
            "location": (p.get("location") or "").strip(),
            "url": (p.get("url") or "").strip(),
            "snippet": (p.get("snippet") or "").strip(),
        })
    return cleaned if len(cleaned) >= 2 else []


# Job boards / ATS platforms that send on behalf of many different employers.
# Never treat these as "the company" for sheet matching purposes.
PLATFORM_SENDER_DOMAINS = {
    "linkedin.com", "indeed.com", "myworkday.com", "ashbyhq.com",
    "greenhouse.io", "lever.co", "mercor.com", "jobalert.indeed.com",
    "em.remotejobs.io", "mg.flexjobs.com", "jobgether.com", "glassdoor.com",
}

# Real incident: qwen2.5:7b reproducibly (3/3 attempts, not a fluke) misclassified
# "Thank you for your application to Warner Music Group" -- an unambiguous ATS
# acknowledgment with zero ambiguity -- as category=job_opportunity with an empty
# company. Combined with the sender being on a known ATS domain (hire.lever.co),
# that emptied-company job_opportunity read triggered the "no identifiable hiring
# company" skip path in main.py, which marks the message read -- permanently
# dropping a real status update with no retry, since nothing else ever revisits an
# already-read message. This phrasing is extremely templated across ATSes (Lever,
# Greenhouse, Workday, etc.) and near-zero-risk to match deterministically, so it
# overrides a wrong LLM category rather than trusting a model this small to get
# every case right.
_APPLICATION_ACK_SUBJECT_RE = re.compile(
    r"thank you for (your interest|applying|your application)"
    r"|(we|thanks).{0,25}received your application"
    r"|your application (to|for|at|has been)"
    r"|your application was (sent|submitted|received)",
    re.IGNORECASE,
)

_TRIAGE_PROMPT = """\
You are triaging an email for a job seeker. Read the email and answer with ONLY valid JSON, no markdown fences:
{{
  "category": "<one of: job_opportunity, application_reply, other>",
  "company": "<the actual HIRING company's name (not a job board/ATS like LinkedIn/Indeed/Greenhouse acting as sender), empty string if not identifiable>",
  "role_title": "<job title if identifiable, else empty string>",
  "position_id": "<explicit requisition/job/position ID stated in the email, e.g. 'R4027488' or '20213', empty string if none stated>",
  "contact_name": "<name of the human who sent/wrote the email, if any, else empty string>",
  "location": "<city/region where the job is based, e.g. 'Haifa', 'Tel Aviv', 'Remote', empty string if not stated>",
  "status": "<if category=application_reply: one of applied, interview, offer, rejected. Else empty string>",
  "notes": "<if category=application_reply: one short sentence summarizing the update. Else empty string>"
}}

category=job_opportunity: recruiter outreach, job alert, or a posting about an open role you have not applied to yet.
category=application_reply: a reply/update about an application you already submitted to a specific company
  (acknowledgment, interview invite, rejection, offer). Must name or clearly imply a specific hiring company,
  not a job-board digest of many companies. This includes interview-scheduling notices even when they don't
  literally say "thank you for applying" -- e.g. a calendar-style logistics email naming a specific interview
  round, requisition ID, role title, and interviewer names (in any language, and even if mostly an embedded
  image with only a few lines of real text) is still an application_reply with status=interview.
category=other: newsletters, marketing, unrelated personal mail, generic digests of many companies.

For status (only if application_reply): "interview" if they want to schedule/conduct an interview, confirm an
interview time/logistics, or move to the next round; "offer" only if an actual job offer is extended;
"rejected" if declining the application; "applied" if it's just a generic acknowledgment with no status change.

EMAIL:
Subject: {subject}
From: {sender_name} <{sender_email}>
Body:
{body}
"""


def classify_email(msg: EmailMessage) -> dict:
    prompt = _TRIAGE_PROMPT.format(
        subject=msg.subject,
        sender_name=msg.sender_name,
        sender_email=msg.sender_email,
        body=(msg.body or msg.snippet)[:2000],
    )
    default = {
        "category": "other", "company": "", "role_title": "", "position_id": "",
        "contact_name": "", "location": "", "status": "", "notes": "",
    }
    result = llm_client.call_json(prompt)  # raises on failure; caller decides retry behavior

    notes = result.get("notes") or ""
    status = result.get("status") or ""
    if guardrails.looks_like_hallucinated_triage(notes, status):
        logger.warning(
            "[SUSPECT LLM OUTPUT] classify_email returned an implausible result "
            "(notes=%d chars, status=%r) for subject=%r from %s -- discarding the "
            "whole result rather than trusting a likely-fabricated status/notes.",
            len(notes), status, msg.subject, msg.sender_email,
        )
        # Discarding to category=other means a real status update in a genuinely
        # verbose reply could get missed -- prefer that (silent no-op, visible here
        # and retried never) over trusting fabricated content, but surface it so a
        # missed real update doesn't just disappear the way the bogus ones did.
        notifier.notify_needs_review(
            f"Discarded an implausible AI triage result for '{msg.subject}' from {msg.sender_email} "
            "(looked like leaked LLM reasoning) -- check this email manually if it was a real reply."
        )
        return default

    default.update(result)

    if default["category"] != "application_reply" and _APPLICATION_ACK_SUBJECT_RE.search(msg.subject or ""):
        logger.info(
            "[SUBJECT OVERRIDE] '%s' -> LLM said category=%r, but subject is an unmistakable "
            "application-acknowledgment pattern; overriding to application_reply/applied.",
            msg.subject, default["category"],
        )
        default["category"] = "application_reply"
        if not default["company"]:
            default["company"] = msg.sender_name
        if not default["status"]:
            default["status"] = "applied"

    return default


_REQUISITION_ID_RE = re.compile(
    r"(?:job\s*requisition\s*id|requisition\s*(?:id|number|#)|req\s*#?|job\s*id)\s*[:#]?\s*([A-Z]{0,4}[-_]?\d{4,10})",
    re.IGNORECASE,
)


def extract_requisition_id(description: str) -> str:
    """The employer's own requisition ID (e.g. 'JR2023080') is more stable than
    LinkedIn's own numeric listing ID -- a role that closes and reopens gets a brand
    new LinkedIn listing ID, but the same requisition ID, so this is the strongest
    possible dedup key when a real fetched job description contains one."""
    match = _REQUISITION_ID_RE.search(description or "")
    return match.group(1).upper() if match else ""


def extract_linkedin_job_id(text: str) -> str:
    """Regex-extracted LinkedIn job ID from any text containing a /jobs/view/<id> URL.
    Used to keep job_id_for consistent across the digest-parser path and the single-
    posting LLM-triage path -- relying on the LLM to report the same ID both times
    was unreliable and caused the same real posting to dedupe under two different keys.
    """
    match = _JOB_ID_IN_URL_RE.search(text)
    return match.group(1) if match else ""


def is_connection_request(msg: EmailMessage) -> bool:
    """LinkedIn 'I want to connect' invites -- fixed, recognizable pattern, never a
    job opportunity. Filtered deterministically rather than relying on the LLM, which
    occasionally misclassified one as a job posting (using the inviter's own listed
    role/company as if it were a job opening)."""
    return msg.subject.strip().lower() in {"i want to connect", "i'd like to join your professional network"}


def is_platform_domain(email_address: str) -> bool:
    domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
    return any(domain == d or domain.endswith("." + d) for d in PLATFORM_SENDER_DOMAINS)


# Job boards/ATS platforms sometimes get extracted AS the "company" itself when the
# LLM can't find the real employer (e.g. Jobgether's own listings never disclose the
# real employer until you apply). Catch these regardless of sender domain -- adding
# "LinkedIn" or "Jobgether" as a tracked "company" pollutes the sheet.
PLATFORM_COMPANY_NAMES = {
    "linkedin", "linkedin job alerts", "indeed", "indeed jobs", "jobgether",
    "flexjobs", "remotejobs", "ziprecruiter", "glassdoor", "monster", "wellfound",
}


def is_platform_company_name(company: str) -> bool:
    normalized = company.strip().lower()
    return normalized in PLATFORM_COMPANY_NAMES


_CONFIRM_COMPANY_PROMPT = """\
You are verifying who the hiring company actually is for a job posting. Read the page text below
and answer with ONLY valid JSON, no markdown fences:
{{
  "company": "<the actual hiring company's name as stated on the page, or \"{guessed_company}\" if the page doesn't clearly state a different one>"
}}

GUESSED COMPANY: {guessed_company}

PAGE TEXT:
{page_text}
"""


# Real incident: for a recruiting-agency posting the model "confirmed" the company as a
# full descriptive sentence ("An innovative startup developing AI-driven predictive
# platforms for continuous, non-invasive health monitoring.") and it was written to the
# sheet as the company. A real company name is short; anything sentence-shaped is the
# model describing the employer instead of naming it, so keep the original guess.
_MAX_COMPANY_NAME_CHARS = 60
_MAX_COMPANY_NAME_WORDS = 6


def _looks_like_company_name(name: str) -> bool:
    return len(name) <= _MAX_COMPANY_NAME_CHARS and len(name.split()) <= _MAX_COMPANY_NAME_WORDS


def confirm_company_name(page_text: str, guessed_company: str) -> str:
    """Asks the LLM to confirm/correct the hiring company name against real fetched
    page text, since the emailed guess (often just the sender) isn't always the real
    employer. Falls back to the original guess on any failure or empty answer."""
    prompt = _CONFIRM_COMPANY_PROMPT.format(guessed_company=guessed_company, page_text=page_text[:2000])
    try:
        result = llm_client.call_json(prompt)
    except Exception:
        logger.exception("Company-name confirmation failed for guess=%r", guessed_company)
        return guessed_company
    confirmed = (result.get("company") or "").strip()
    if not confirmed or not _looks_like_company_name(confirmed):
        return guessed_company
    return confirmed


# junior/intern/student-level roles are never relevant regardless of fit score.
_JUNIOR_TITLE_RE = re.compile(
    r"\b(intern(?:ship)?|junior|jr\.?|student|entry[\s-]?level|co-?op|apprentice|trainee|working student)\b",
    re.IGNORECASE,
)


def is_junior_or_intern_title(title: str) -> bool:
    return bool(_JUNIOR_TITLE_RE.search(title or ""))


def is_location_excluded(location: str) -> bool:
    """Commute filter -- see config.ALLOWED_LOCATION_KEYWORDS/EXCLUDED_LOCATION_KEYWORDS
    to customize for your own commute range. Deliberately conservative: only a
    location that clearly matches a known out-of-range city is excluded -- blank,
    remote, or unrecognized locations are never filtered, since there's no honest
    basis to reject a role we can't actually place."""
    low = (location or "").strip().lower()
    if not low:
        return False
    if any(city in low for city in config.ALLOWED_LOCATION_KEYWORDS):
        return False
    return any(city in low for city in config.EXCLUDED_LOCATION_KEYWORDS)
