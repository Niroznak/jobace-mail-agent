"""Local dedup state: which Gmail message IDs have already been processed."""
from __future__ import annotations

import csv
import json
import os
from datetime import date

from . import config


def load_processed_ids() -> set[str]:
    if os.path.exists(config.PROCESSED_IDS_PATH):
        with open(config.PROCESSED_IDS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids: set[str]) -> None:
    with open(config.PROCESSED_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def load_grayed_job_ids() -> set[str]:
    if os.path.exists(config.GRAYED_JOB_IDS_PATH):
        with open(config.GRAYED_JOB_IDS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_grayed_job_ids(job_ids: set[str]) -> None:
    with open(config.GRAYED_JOB_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(job_ids), f, indent=2)


def load_pending_verification() -> dict[str, int]:
    """job_id -> attempt count, for stage 3's give-up-and-drop retry tracking."""
    if os.path.exists(config.PENDING_VERIFICATION_PATH):
        with open(config.PENDING_VERIFICATION_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_pending_verification(pending: dict[str, int]) -> None:
    with open(config.PENDING_VERIFICATION_PATH, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2)


def load_seen_career_postings() -> dict[str, list[str]]:
    if os.path.exists(config.SEEN_CAREER_POSTINGS_PATH):
        with open(config.SEEN_CAREER_POSTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen_career_postings(seen: dict[str, list[str]]) -> None:
    with open(config.SEEN_CAREER_POSTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


_SKIPPED_FIELDNAMES = ["date", "company", "title", "reason", "score", "url", "summary"]


def log_skipped_candidate(company: str, title: str, reason: str, score="", url: str = "", summary: str = "") -> None:
    """Recoverable record of a candidate that was scored/evaluated but never written
    to the sheet (low fit, hard-requirement cap). Daily log files rotate and are easy
    to lose track of -- a wrong LLM judgment on a genuinely good role would otherwise
    be unrecoverable once that log's date scrolls out of memory. Append-only, never
    read by any pipeline logic -- purely a human-inspectable audit trail."""
    file_exists = os.path.exists(config.SKIPPED_CANDIDATES_PATH)
    with open(config.SKIPPED_CANDIDATES_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_SKIPPED_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "date": date.today().isoformat(), "company": company, "title": title,
            "reason": reason, "score": score, "url": url, "summary": summary,
        })


def log_skipped_detail(record: dict) -> None:
    """Append one JSON line with everything needed to re-score or audit a skipped
    candidate later: the exact text that was scored, the extracted requirements with CV
    coverage, the score breakdown and the human-readable reasoning. Never read by pipeline
    logic (see log_skipped_candidate) -- purely an audit/assessment trail."""
    record = {"date": date.today().isoformat(), **record}
    with open(config.SKIPPED_DETAIL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
