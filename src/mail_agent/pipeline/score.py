"""Stage 4: score a verified position against the candidate's CV profile. Below
config.FIT_SCORE_THRESHOLD, logs to skipped_candidates.csv (recoverable if a wrong
LLM judgment excluded a genuinely good role) and returns None -- never reaches
stage 5, no sheet write.
"""
from __future__ import annotations

import json
import logging

from .. import config
from .. import cv_matcher
from .. import state
from .types import ScoredItem, VerifiedPosition

logger = logging.getLogger(__name__)


def score_position(verified: VerifiedPosition, description_score_chars: int | None = None) -> ScoredItem | None:
    company, title = verified.company, verified.title
    content = verified.description[: description_score_chars or config.DESCRIPTION_SCORE_CHARS]
    result = cv_matcher.score_job_email(company, title, content)
    score = result.get("score", -1)

    if score < config.FIT_SCORE_THRESHOLD:
        logger.info("[SCORE] '%s @ %s' score=%s < threshold, skipping sheet write.", title, company, score)
        state.log_skipped_candidate(company, title, "LOW_FIT", score, verified.url, result.get("summary", ""))
        return None

    logger.info("[SCORE] '%s @ %s' score=%s -> passed, queued for reconciliation.", title, company, score)
    checked = result.get("requirements_checked") or []
    return ScoredItem(
        verified=verified, score=score, summary=result.get("summary", ""),
        requirements_json=json.dumps(checked, ensure_ascii=False, separators=(",", ":")) if checked else "",
    )
