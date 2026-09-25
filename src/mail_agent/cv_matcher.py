"""Load/cache the CV profile (reusing job seek's cv.txt) and score job emails against it."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from . import job_page_fetcher
from . import llm_client
from . import config
from . import requirements_check
from . import skill_lexicon

logger = logging.getLogger(__name__)

_PARSE_PROMPT = """\
You are an expert CV parser. Extract structured information and return ONLY valid JSON, no markdown fences.

Extract the following from the CV and return as JSON:
{
  "summary": "<2-sentence professional summary>",
  "seniority": "<junior|mid|senior|lead|staff>",
  "total_years_experience": <integer>,
  "technical_skills": {
    "languages": [<programming languages>],
    "frameworks": [<frameworks/libraries>],
    "domains": [<subfields, e.g. nlp, backend, data engineering>],
    "tools": [<non-language tools>],
    "databases": [<database systems>]
  },
  "domains": [<industry/application domains>],
  "work_history": [
    {"title": "", "company": "", "years": "", "highlights": [<key tech/skills>]}
  ],
  "education": [{"degree": "", "field": "", "institution": "", "year": <int>}]
}

CV TEXT:
"""

_SCORE_PROMPT = """\
You are a senior technical recruiter. Distinguish "required" from "preferred" in job postings.
Be conservative: mark as maybe_gap when uncertain rather than must_have_met.

Extract the role's requirements; the candidate's fit is checked in code against the real CV, so
do not judge blockers yourself. For EACH stated requirement give:
  "text": the requirement, "kind": one of language | technology | domain | degree | years | soft,
  "necessity": "must_have" (must/required/mandatory/minimum) or "nice_to_have" (preferred/advantage/plus),
  "any_of": every acceptable keyword for it -- if the posting offers alternatives ("Python, C++ or Java")
  list them all; for a domain give the field's names (e.g. ["chip design","ASIC","SoC"]).
Split compound sentences into one requirement per skill ("ML and data analysis" -> two items). "any_of"
must be short keywords (1-2 words each) copied from THIS posting's own wording (never from the candidate's profile) that would also literally appear in a CV -- never a sentence, never empty
for a language/technology/domain requirement.
Cover programming languages, technologies/tools and the professional domain/field the role demands.

Score ONLY against this specific role's own stated responsibilities and requirements section --
never against generic "About the company" / mission-statement / marketing text elsewhere on the
page. A company that builds AI or software products can still post an unrelated non-technical role
(logistics, HR, sales, quality/supply-chain operations, finance) -- the fact that the surrounding
page is full of the company's own AI/software buzzwords is not evidence that THIS role involves any
of it (real case: an AI/software company's page describing itself as an "AI-powered" business
scored 83/100 for a "Dealer Part Return Repair/Reuse Management" role whose actual listed duties
were entirely supply-chain/quality-control -- zero software or AI content -- because the company
boilerplate around it was saturated with AI/data-analytics language having nothing to do with this
specific job's responsibilities). If the role's own responsibilities don't involve the candidate's
core skills, score low regardless of how much unrelated company-level buzzword text surrounds it.

CANDIDATE PROFILE:
{cv_profile}

JOB EMAIL:
Company: {company}
Title: {title}
Content: {content}

Respond with ONLY valid JSON (no markdown fences):
{{
  "score": <integer 0-100>,
  "strengths": [<candidate skills/experience that directly match this job>],
  "requirements": [{{"text": "", "kind": "", "necessity": "", "any_of": []}}],
  "must_have_gaps": [<required skills the candidate is missing>],
  "hard_requirement_gaps": [],
  "summary": "<one concise sentence verdict>"
}}

Score guide: 80-100 strong match, 60-79 good, 40-59 partial, <40 poor fit\
"""


def _cv_hash() -> str | None:
    if not os.path.exists(config.CV_TEXT_PATH):
        return None
    with open(config.CV_TEXT_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _load_cache() -> dict:
    if os.path.exists(config.CV_PROFILE_CACHE_PATH):
        with open(config.CV_PROFILE_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def ensure_profile() -> dict:
    """Return cached CV profile, re-parsing via `claude --print` only if cv.txt changed."""
    current_hash = _cv_hash()
    if current_hash is None:
        raise FileNotFoundError(f"CV text file not found at '{config.CV_TEXT_PATH}'.")

    cache = _load_cache()
    if cache.get("_cv_hash") == current_hash and "profile" in cache:
        return cache["profile"]

    with open(config.CV_TEXT_PATH, "r", encoding="utf-8") as f:
        cv_text = f.read()

    logger.info("CV changed or not cached, parsing via Ollama (this can take a few minutes on first run)...")
    profile = llm_client.call_json(
        _PARSE_PROMPT + cv_text,
        timeout=config.OLLAMA_CV_PARSE_TIMEOUT_SECONDS,
        model=config.OLLAMA_MODEL_CV_PARSE,
        num_predict=2000,  # full structured profile -- genuinely needs more room than a triage/score response
    )

    with open(config.CV_PROFILE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"_cv_hash": current_hash, "profile": profile}, f, indent=2, ensure_ascii=False)

    return profile


_NOT_A_POSTING_RESULT = {
    "score": 0, "strengths": [], "must_have_gaps": [], "hard_requirement_gaps": [],
    "summary": "Content doesn't look like a real job posting (no requirements/responsibilities/"
               "qualifications language found) -- skipped scoring rather than judging fitness "
               "against text that likely isn't a job description at all.",
}

# Real incident: a posting stating "US Citizens only" plainly, well within the
# scored content window, still got scored 72/100 by the LLM -- it simply didn't
# weight an explicit, unambiguous eligibility restriction as disqualifying. Unlike
# a skill gap (still a matter of degree, correctly left to the LLM + the hard-
# requirement cap below), citizenship/work-authorization is binary and detectable
# by pattern alone: enforced here in code, the same reasoning already applied to
# _apply_hard_requirement_cap -- never trust the model's own arithmetic/weighting
# on a deterministic disqualifier when a reliable pattern exists.
_CITIZENSHIP_RESTRICTION_RE = re.compile(
    r"\b(u\.?s\.?a?|united states)\s+citizens?\s+only\b"
    r"|\bmust\s+be\s+(a\s+)?(u\.?s\.?a?|united states)\s+citizen\b"
    r"|\b(u\.?s\.?a?|united states)\s+citizenship\s+(is\s+)?required\b"
    r"|\bno\s+(visa\s+)?sponsorship\b"
    r"|\bunable\s+to\s+sponsor\b"
    r"|\bwill\s+not\s+sponsor\b"
    r"|\bsecurity\s+clearance\b[^.]{0,80}\bcitizen",
    re.IGNORECASE,
)
_CITIZENSHIP_INELIGIBLE_RESULT = {
    "score": 0, "strengths": [], "must_have_gaps": ["US citizenship / work authorization"],
    "hard_requirement_gaps": ["US citizenship / work authorization"],
    "summary": "Posting restricts to US citizens / requires US work authorization with no sponsorship -- an "
               "absolute eligibility blocker regardless of skill fit, so score is forced to 0 in code rather "
               "than left to the LLM's own judgment.",
}


def _has_citizenship_restriction(content: str) -> bool:
    return bool(_CITIZENSHIP_RESTRICTION_RE.search(content or ""))


def score_job_email(company: str, title: str, content: str) -> dict:
    # Pre-scoring sanity gate, no LLM call: catches content that was fetched
    # successfully but isn't actually a job posting -- e.g. a JS-rendered page's
    # bootstrap JSON leaking through, or a real page (nav chrome + product blurb)
    # that just isn't a job listing. The LLM will confidently score whatever it's
    # handed; this is the check that stops garbage content from ever reaching it.
    if not job_page_fetcher.looks_like_job_posting(content):
        logger.info("[NOT A POSTING] '%s @ %s' -> content has no job-posting signal words, skipping LLM scoring.", title, company)
        return dict(_NOT_A_POSTING_RESULT)

    if _has_citizenship_restriction(content):
        logger.info("[INELIGIBLE] '%s @ %s' -> citizenship/work-authorization restriction, score forced to 0.", title, company)
        return dict(_CITIZENSHIP_INELIGIBLE_RESULT)

    profile = ensure_profile()
    prompt = _SCORE_PROMPT.format(
        cv_profile=json.dumps(profile, ensure_ascii=False),
        company=company,
        title=title,
        content=job_page_fetcher.focus_description(content, config.DESCRIPTION_SCORE_CHARS),
    )
    result = llm_client.call_json(prompt, num_predict=1500, deterministic=True)  # raises on failure; caller decides retry behavior
    result = _apply_hard_requirement_cap(_apply_requirements_check(result, content))
    if result.get("score_breakdown"):
        result["score_reasoning"] = requirements_check.explain(
            result["score_breakdown"], result.get("hard_requirement_gaps") or [],
            result.get("score", 0), result.get("model_score"),
        )
    return result


def _cv_text() -> str:
    try:
        with open(config.CV_TEXT_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _apply_requirements_check(result: dict, posting_text: str = "") -> dict:
    """Replaces the model's own opinion of blockers with the code-checked result: every
    required language/technology/domain is matched against the real CV text."""
    # Lexicon first (repeatable, never forgets a known term), LLM items only where they add
    # something new.
    reqs = skill_lexicon.merge_requirements(
        skill_lexicon.extract_requirements(posting_text),
        result.get("requirements") if isinstance(result.get("requirements"), list) else [],
    )
    if not reqs:
        return result
    cv = _cv_text()
    if not cv:
        return result
    blocking, other = requirements_check.evaluate(reqs, cv, posting_text)
    breakdown = requirements_check.score_breakdown(reqs, cv, posting_text)
    if breakdown is not None:
        result["model_score"] = result.get("score")
        result["score"] = breakdown["score"]
        result["score_breakdown"] = breakdown
    result["requirements_checked"] = requirements_check.annotate(reqs, cv, posting_text)
    result["hard_requirement_gaps"] = blocking
    result["must_have_gaps"] = blocking + other
    return result


def _apply_hard_requirement_cap(result: dict) -> dict:
    """Enforced in code, not left to the LLM's own arithmetic: a genuine hard-
    requirement gap (a sole, non-substitutable required skill the candidate lacks,
    e.g. "strong Java expertise required") caps the score below FIT_SCORE_THRESHOLD
    so the posting still falls out through the existing low-fit filter, rather than
    trusting the model to self-limit the number it just picked."""
    hard_gaps = result.get("hard_requirement_gaps") or []
    if not hard_gaps:
        return result
    original_score = result.get("score", 0)
    capped_score = min(original_score, config.HARD_REQUIREMENT_SCORE_CAP)
    if capped_score != original_score:
        note = f"Score capped {original_score}->{capped_score}: missing hard requirement(s): {', '.join(hard_gaps)}."
        result["summary"] = f"{note} {result.get('summary', '')}".strip()
        result["score"] = capped_score
    return result
