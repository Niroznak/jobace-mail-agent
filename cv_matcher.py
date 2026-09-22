"""Load/cache the CV profile (reusing job seek's cv.txt) and score job emails against it."""
from __future__ import annotations

import hashlib
import json
import logging
import os

import job_page_fetcher
import llm_client
import config

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

Pay special attention to HARD requirements: a specific, non-substitutable skill/technology stated
as mandatory that the candidate does not have. Do NOT treat something as a hard requirement if the
posting offers acceptable alternatives and the candidate has at least one of them -- e.g. "experience
in a programming language like C++, Python, or Java" is satisfied by knowing any one of those, even
without Java specifically. Only list a skill in hard_requirement_gaps when it is stated as the sole
required option with no listed alternative the candidate meets (e.g. "must have Java", "strong Java
expertise required", "5+ years of Java required").

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
  "must_have_gaps": [<required skills the candidate is missing>],
  "hard_requirement_gaps": [<subset of must_have_gaps that are strict, non-substitutable required skills with no acceptable alternative the candidate has -- empty list if none>],
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


def score_job_email(company: str, title: str, content: str) -> dict:
    # Pre-scoring sanity gate, no LLM call: catches content that was fetched
    # successfully but isn't actually a job posting -- e.g. a JS-rendered page's
    # bootstrap JSON leaking through, or a real page (nav chrome + product blurb)
    # that just isn't a job listing. The LLM will confidently score whatever it's
    # handed; this is the check that stops garbage content from ever reaching it.
    if not job_page_fetcher.looks_like_job_posting(content):
        logger.info("[NOT A POSTING] '%s @ %s' -> content has no job-posting signal words, skipping LLM scoring.", title, company)
        return dict(_NOT_A_POSTING_RESULT)

    profile = ensure_profile()
    prompt = _SCORE_PROMPT.format(
        cv_profile=json.dumps(profile, ensure_ascii=False),
        company=company,
        title=title,
        content=content[:config.DESCRIPTION_SCORE_CHARS],
    )
    result = llm_client.call_json(prompt)  # raises on failure; caller decides retry behavior
    return _apply_hard_requirement_cap(result)


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
