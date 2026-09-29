"""Run ONE specific email through every pipeline stage in isolation, printing each
stage's own output and reasoning as it goes. NEVER writes anything (reconcile always
runs with dry_run=True) -- purely for answering "why did/didn't this specific posting
make it into the sheet" without running a full batch and without touching real state.

This is the tool for the "each section independent and debuggable" request: EXTRACT,
VERIFY, and SCORE are each a separately-tested, typed function (src/mail_agent/pipeline/)
-- this script just calls them one at a time on one real message and shows you exactly
where a given item stops and why, instead of needing to run the whole batch and grep logs.

Usage:
    python debug_pipeline.py "<gmail search query>"   # e.g. a company name, subject text
    python debug_pipeline.py --id <gmail message id>  # exact message, if you have the id

Examples:
    python debug_pipeline.py "JLL"
    python debug_pipeline.py "Senior ML" --max 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config, gmail_client, sheets_client, state
from mail_agent.pipeline import extract, reconcile, score, verify
from mail_agent.pipeline.types import Candidate

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.DEBUG, format="  [%(name)s] %(message)s")
_SEP = "=" * 80


def _print_candidate(c: Candidate, index: int) -> None:
    print(f"\n--- Candidate {index}: kind={c.kind} source={c.source} ---")
    print(f"  Company: {c.company!r}")
    print(f"  Title:   {c.title!r}")
    if c.url:
        print(f"  URL:     {c.url[:100]}")
    if c.status_signal:
        print(f"  Status signal (reply): {c.status_signal!r}")


def debug_one_message(msg, sheet_rows: list[dict], retry_state: dict) -> None:
    print(f"\n{_SEP}\nMESSAGE id={msg.id}\nFrom: {msg.sender_name} <{msg.sender_email}>\nSubject: {msg.subject}\n{_SEP}")

    print("\n[STAGE 1: EXTRACT] -- what does this email structurally contain?")
    try:
        candidates = extract.extract_candidates(msg, sheet_rows)
    except Exception as exc:
        print(f"  EXTRACT RAISED: {exc.__class__.__name__}: {exc}")
        return
    if not candidates:
        print("  -> 0 candidates. Nothing further to check for this message.")
        return
    print(f"  -> {len(candidates)} candidate(s) extracted.")

    for i, c in enumerate(candidates, 1):
        _print_candidate(c, i)

        if c.kind == "reply":
            print("\n  [STAGE 5: RECONCILE] (replies skip verify/score) -- dry-run, nothing written")
            try:
                result = reconcile.reconcile(c, sheet_rows, None, dry_run=True)
                print(f"  -> action={result.action}  detail={result.detail}")
            except Exception as exc:
                print(f"  RECONCILE RAISED: {exc.__class__.__name__}: {exc}")
            continue

        print("\n  [STAGE 3: VERIFY] -- is this a real, live, grounded, non-duplicate position?")
        try:
            verified = verify.verify_position(c, sheet_rows, retry_state)
        except Exception as exc:
            print(f"  VERIFY RAISED: {exc.__class__.__name__}: {exc}")
            continue
        if verified is None:
            print("  -> None (see [VERIFY] log line above for the exact reason).")
            continue
        print(f"  -> Verified. Company confirmed as: {verified.company!r}")
        print(f"     Description length: {len(verified.description)} chars")
        print(f"     Description preview: {verified.description[:200]!r}")

        print("\n  [STAGE 4: SCORE] -- fit against the CV, with full reasoning")
        try:
            scored = score.score_position(verified)
        except Exception as exc:
            print(f"  SCORE RAISED: {exc.__class__.__name__}: {exc}")
            continue
        if scored is None:
            print(f"  -> None (below {config.FIT_SCORE_THRESHOLD}; see data/skipped_detail.jsonl for full reasoning).")
            continue
        print(f"  -> Score: {scored.score}/100")
        print(f"     Summary: {scored.summary}")

        print("\n  [STAGE 5: RECONCILE] -- dry-run, nothing written")
        try:
            result = reconcile.reconcile(scored, sheet_rows, None, dry_run=True)
            print(f"  -> action={result.action}  detail={result.detail}")
        except Exception as exc:
            print(f"  RECONCILE RAISED: {exc.__class__.__name__}: {exc}")


def main(query: str | None, message_id: str | None, max_results: int) -> None:
    gmail = gmail_client.get_gmail_service()
    sheets = sheets_client.get_sheets_service()
    sheet_rows = sheets_client.fetch_all_rows(sheets)
    retry_state = state.load_pending_verification()

    if message_id:
        msg = gmail_client.fetch_message(gmail, message_id)
        debug_one_message(msg, sheet_rows, retry_state)
        return

    resp = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    print(f"Found {len(ids)} message(s) matching query={query!r}")
    for mid in ids:
        msg = gmail_client.fetch_message(gmail, mid)
        debug_one_message(msg, sheet_rows, retry_state)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="Gmail search query (company name, subject text, etc.)")
    ap.add_argument("--id", dest="message_id", help="Exact Gmail message id instead of a search query")
    ap.add_argument("--max", type=int, default=5, help="Max messages to fetch for a query (default 5)")
    args = ap.parse_args()
    if not args.query and not args.message_id:
        ap.error("Provide a search query or --id")
    main(args.query, args.message_id, args.max)
