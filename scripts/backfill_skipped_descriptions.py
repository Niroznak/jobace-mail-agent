"""One-off sweep: re-fetch the posting text for every entry in skipped_candidates.csv whose
link still works, and store it in data/skipped_detail.jsonl -- the same file the pipeline
now writes for every new skip -- so scoring/reasoning tests can be run offline against a
fixed dataset (links die; this snapshots what is still reachable).

No LLM calls (pure page fetches), so it doesn't touch the shared Ollama. Idempotent:
entries already present in the detail file (found or failed) are skipped; pass
--retry-failed to re-attempt the ones that couldn't be fetched last time.

Usage:
    python backfill_skipped_descriptions.py [--retry-failed] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config, job_page_fetcher, state
from mail_agent.pipeline.verify import MIN_DIGEST_DESCRIPTION_CHARS

logging.basicConfig(level=logging.WARNING, format="%(message)s")
_DELAY_SECONDS = 1.0  # polite gap between fetches (LinkedIn/Indeed throttle bursts)
_A = lambda s: str(s).encode("ascii", "replace").decode()


def _load_done(retry_failed: bool) -> set[str]:
    done: set[str] = set()
    if not os.path.exists(config.SKIPPED_DETAIL_PATH):
        return done
    with open(config.SKIPPED_DETAIL_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("fetch") == "failed" and retry_failed:
                continue
            done.add(rec.get("url", ""))
    return done


def main(retry_failed: bool, limit: int | None) -> None:
    with open(config.SKIPPED_CANDIDATES_PATH, encoding="utf-8", newline="") as f:
        entries = list(csv.DictReader(f))
    done = _load_done(retry_failed)
    seen: set[str] = set()
    todo = []
    for e in entries:
        url = (e.get("url") or "").strip()
        if not url or url in done or url in seen:
            continue
        seen.add(url)
        todo.append(e)
    if limit:
        todo = todo[:limit]
    print(f"{len(entries)} logged skips -> {len(todo)} unique link(s) to fetch")

    found = failed = 0
    for i, e in enumerate(todo, 1):
        url = e["url"].strip()
        text = job_page_fetcher.refetch_full_description(url)
        usable = len(text) >= MIN_DIGEST_DESCRIPTION_CHARS and job_page_fetcher.looks_like_job_posting(text)
        record = {
            "date": e.get("date", ""), "company": e.get("company", ""), "title": e.get("title", ""),
            "url": url, "score": e.get("score", ""), "summary": e.get("summary", ""), "backfilled": True,
        }
        if usable:
            record["scored_text"] = job_page_fetcher.focus_description(text, config.DESCRIPTION_SCORE_CHARS)
            found += 1
        else:
            record.update({"scored_text": "", "fetch": "failed"})
            failed += 1
        state.log_skipped_detail(record)
        print(f"[{i}/{len(todo)}] {'OK  ' if usable else 'gone'} {_A(e.get('title',''))[:45]} @ {_A(e.get('company',''))[:20]}", flush=True)
        time.sleep(_DELAY_SECONDS)
    print(f"\nDone: {found} description(s) saved, {failed} no longer reachable.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    main(a.retry_failed, a.limit)
