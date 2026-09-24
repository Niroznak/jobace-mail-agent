"""Deterministic CV-vs-requirements check. The LLM only *extracts* a posting's
requirements (text, kind, necessity, alternative keywords); whether the candidate has
each one is decided here by matching against the real CV text, and what counts as a
blocker is fixed policy, not model judgment.

Real incident: a chip-design / SoC-verification role scored 73 because the model listed
those gaps in its summary but still picked a high number, and the "hard requirement"
wording was too narrow to catch domain gaps.

Policy:
  * required language / technology / domain the CV lacks -> blocker (score capped)
  * any of the listed alternatives present in the CV      -> satisfied (python/c++/java)
  * degree, years-of-experience, soft skills, "preferred" -> never a blocker
"""
from __future__ import annotations

import re

BLOCKING_KINDS = {"language", "technology", "domain"}

# Short forms a CV and a posting may use for the same thing.
_ALIASES = {
    "ml": "machine learning", "dl": "deep learning", "cv": "computer vision",
    "nlp": "natural language processing", "js": "javascript", "ts": "typescript",
    "k8s": "kubernetes", "golang": "go", "postgres": "postgresql", "c plus plus": "c++",
}


def _norm(text: str) -> str:
    text = (text or "").lower()
    for short, full in _ALIASES.items():
        text = re.sub(rf"(?<![\w+#]){re.escape(short)}(?![\w+#])", full, text)
    return text


def _term_in_cv(term: str, cv_norm: str) -> bool:
    term = _norm(term).strip()
    if not term:
        return False
    return re.search(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", cv_norm) is not None


def evaluate(requirements: list[dict], cv_text: str) -> tuple[list[str], list[str]]:
    """Returns (blocking_gaps, other_gaps) as human-readable requirement texts."""
    cv_norm = _norm(cv_text)
    blocking, other = [], []
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        text = str(req.get("text") or "").strip()
        keywords = [str(k) for k in (req.get("any_of") or []) if k]
        if not text:
            continue
        if not keywords:
            # Nothing concrete to look up in the CV -- can't verify, so never a blocker.
            other.append(text)
            continue
        if any(_term_in_cv(k, cv_norm) for k in keywords):
            continue
        kind = str(req.get("kind") or "").lower()
        required = str(req.get("necessity") or "").lower() == "required"
        (blocking if required and kind in BLOCKING_KINDS else other).append(text)
    return blocking, other
