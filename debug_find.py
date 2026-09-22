"""One-off debug: find a specific email by query, run it through triage + scoring, print results."""
from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import classifier
import cv_matcher
import gmail_client

query = sys.argv[1] if len(sys.argv) > 1 else "SCD"

service = gmail_client.get_gmail_service()
resp = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
ids = [m["id"] for m in resp.get("messages", [])]
print(f"Found {len(ids)} messages matching query={query!r}")

for msg_id in ids:
    raw = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    msg = gmail_client._parse_message(raw)
    print("\n" + "=" * 80)
    print(f"id={msg.id}")
    print(f"From: {msg.sender_name} <{msg.sender_email}>")
    print(f"Subject: {msg.subject}")
    print(f"Body (first 500 chars):\n{(msg.body or msg.snippet)[:500]}")

    print("\n-- Triage (Ollama) --")
    try:
        triage = classifier.classify_email(msg)
        print(json.dumps(triage, indent=2, ensure_ascii=False))
    except Exception as exc:
        print("TRIAGE FAILED:", exc)
        continue

    if triage.get("category") == "job_opportunity":
        print("\n-- CV Score (Ollama) --")
        try:
            score = cv_matcher.score_job_email(
                triage.get("company", ""), triage.get("role_title", ""), msg.body or msg.snippet
            )
            print(json.dumps(score, indent=2, ensure_ascii=False))
        except Exception as exc:
            print("SCORING FAILED:", exc)
