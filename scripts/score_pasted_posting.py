"""Score a posting you found and opened yourself (e.g. one LinkedIn/Indeed currently
blocks fetching automatically) by pasting its text in -- reuses the exact same scoring
pipeline (skill lexicon, requirement checking, reasoning) as the automated path, just
skips the network fetch. Never writes to the sheet -- prints the score and full
reasoning for you to review; add it to the sheet yourself if it's a good fit.

Usage:
    python score_pasted_posting.py "Company Name" "Job Title"
    (then paste the posting text, finish with Ctrl+Z Enter on Windows / Ctrl+D on Unix)

    Or from a file:
    python score_pasted_posting.py "Company Name" "Job Title" --file posting.txt
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import cv_matcher, job_page_fetcher

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main(company: str, title: str, text: str) -> None:
    focused = job_page_fetcher.focus_description(text, 5000)
    result = cv_matcher.score_job_email(company, title, focused)

    print(f"\n{'=' * 70}\n{title} @ {company}\n{'=' * 70}")
    print(f"\nScore: {result.get('score', 0)}/100")
    print(f"Summary: {result.get('summary', '')}")
    if result.get("hard_requirement_gaps"):
        print(f"\nBlockers: {', '.join(result['hard_requirement_gaps'])}")
    print("\nReasoning:")
    for line in result.get("score_reasoning") or []:
        print(f"  - {line}")
    print("\nExtracted requirements:")
    for req in result.get("requirements_checked") or []:
        met = "MET" if req["met"] else ("gap" if req["met"] is False else "?  ")
        print(f"  [{met}] {req['level']:12} {req['skill']}")
    print(
        "\nNot written to the sheet -- if this looks right, add it yourself, or paste "
        "the score/reasoning above and ask to have it added."
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("title")
    ap.add_argument("--file", help="Read the posting text from this file instead of stdin")
    args = ap.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            posting_text = f.read()
    else:
        print("Paste the posting text, then press Ctrl+Z then Enter (Windows) or Ctrl+D (Unix):\n")
        posting_text = sys.stdin.read()

    if not posting_text.strip():
        print("No text provided.")
        sys.exit(1)

    main(args.company, args.title, posting_text)
