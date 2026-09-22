"""Read-only: for every message in the Work label, show full triage + score reasoning."""
from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import classifier
import config
import cv_matcher
import gmail_client

service = gmail_client.get_gmail_service()
ids = gmail_client.list_recent_ids(service, newer_than_days=config.MAIL_LOOKBACK_DAYS)
messages = [gmail_client.fetch_message(service, i) for i in ids]
print(f"Scanning {len(messages)} message(s) in label:{config.WORK_LABEL_NAME}\n")

for msg in messages:
    try:
        triage = classifier.classify_email(msg)
    except Exception as exc:
        print(f"[TRIAGE FAILED] {msg.subject!r}: {exc}")
        continue

    if triage.get("category") != "job_opportunity":
        continue

    company = triage.get("company") or msg.sender_name or msg.sender_email
    if not company and classifier.is_platform_domain(msg.sender_email):
        continue
    title = triage.get("role_title") or msg.subject
    content = msg.body or msg.snippet

    try:
        score_result = cv_matcher.score_job_email(company, title, content)
    except Exception as exc:
        print(f"[SCORE FAILED] {title!r} @ {company!r}: {exc}")
        continue

    score = score_result.get("score", -1)
    marker = "MATCH" if score >= config.FIT_SCORE_THRESHOLD else "low fit"
    print("=" * 90)
    print(f"[{marker}] {title} @ {company}  --  score={score}")
    print(f"  strengths: {score_result.get('strengths', [])}")
    print(f"  gaps:      {score_result.get('must_have_gaps', [])}")
    print(f"  summary:   {score_result.get('summary', '')}")
    print()
