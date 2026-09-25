"""Manual (never scheduled) calibration of the fit scoring against YOUR OWN decisions.

Ground truth from the sheet:
  negatives  = rows you marked `nr` by hand ("doesn't fit me"); rows auto-marked by the
               21-day stale rule ("[AUTO]" in notes) are excluded -- not a fit judgement
  positives  = rows you applied to (applied / interview / offer / ATS_reject)

Every row with a usable stored description is re-scored with the CURRENT scoring code,
then reported:
  * false positives : nr rows that would still pass FIT_SCORE_THRESHOLD
  * false negatives : rows you applied to that would now be rejected
  * score distribution per group + the skills your nr rows demand that your CV lacks

Results are cached in data/calibration_results.json so the report can be re-read without
re-scoring:   python calibrate_scoring.py --report-only

Usage:
    python calibrate_scoring.py [--report-only] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config, cv_matcher, job_page_fetcher, sheets_client
from mail_agent.pipeline.verify import MIN_DIGEST_DESCRIPTION_CHARS

logging.basicConfig(level=logging.WARNING, format="%(message)s")
RESULTS_PATH = os.path.join(config.DATA_DIR, "calibration_results.json")
POSITIVE_STATUSES = {"applied", "interview", "offer", "ats_reject"}
_A = lambda s: str(s).encode("ascii", "replace").decode()


def _group(row: dict) -> str | None:
    status = (row.get("status") or "").strip().lower()
    if status == "nr" and "[AUTO]" not in (row.get("notes") or ""):
        return "negative"
    if status in POSITIVE_STATUSES:
        return "positive"
    return None


def score_all(limit: int | None) -> list[dict]:
    rows = sheets_client.fetch_all_rows(sheets_client.get_sheets_service())
    results = []
    for r in rows:
        group = _group(r)
        if not group:
            continue
        desc = r.get("description") or ""
        if len(desc) >= config.DESCRIPTION_STORE_CHARS - 100:  # stored text was cut -- re-read the full posting
            full = job_page_fetcher.refetch_full_description(r.get("url", ""))
            if len(full) > len(desc):
                desc = full
        if len(desc) < MIN_DIGEST_DESCRIPTION_CHARS:
            continue
        if limit and len(results) >= limit:
            break
        try:
            res = cv_matcher.score_job_email(r.get("company", ""), r.get("title", ""), desc)
        except Exception as exc:
            print(f"row {r['_row']}: scoring failed ({exc.__class__.__name__})")
            continue
        checked = res.get("requirements_checked") or []
        results.append({
            "row": r["_row"], "group": group, "title": r.get("title", ""), "company": r.get("company", ""),
            "status": r.get("status", ""), "old_score": r.get("fit_score"), "score": res.get("score", 0),
            "blockers": res.get("hard_requirement_gaps") or [], "requirements": checked,
            "reasoning": res.get("score_reasoning") or [], "text": desc,
        })
        print(f"row {r['_row']:>3} [{group[:3]}] {res.get('score', 0):>3}  {_A(r.get('title',''))[:40]} @ {_A(r.get('company',''))[:18]}")
    if os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH, encoding="utf-8") as f:
            previous = {r["row"]: r["score"] for r in json.load(f)}
        flips = [(r["row"], previous[r["row"]], r["score"]) for r in results
                 if r["row"] in previous and abs(previous[r["row"]] - r["score"]) > 10]
        print(f"\nSTABILITY vs previous run: {len(flips)} row(s) moved by >10 points")
        for row, old, new in flips:
            print(f"  row {row}: {old} -> {new}")
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    return results


def report(results: list[dict]) -> None:
    thr = config.FIT_SCORE_THRESHOLD
    neg = [r for r in results if r["group"] == "negative"]
    pos = [r for r in results if r["group"] == "positive"]
    print(f"\n=== Calibration: {len(neg)} nr (hand-marked) vs {len(pos)} applied-family rows, threshold {thr} ===")
    for name, grp in (("nr", neg), ("applied", pos)):
        if grp:
            sc = [r["score"] for r in grp]
            print(f"{name:8} mean={statistics.mean(sc):.0f} median={statistics.median(sc):.0f} "
                  f"min={min(sc)} max={max(sc)}  pass={sum(s >= thr for s in sc)}/{len(sc)}")
    fp = sorted((r for r in neg if r["score"] >= thr), key=lambda r: -r["score"])
    fn = sorted((r for r in pos if r["score"] < thr), key=lambda r: r["score"])
    print(f"\nFALSE POSITIVES (you marked nr, scoring would pass): {len(fp)}")
    for r in fp:
        print(f"  row {r['row']} score={r['score']} {_A(r['title'])[:45]} @ {_A(r['company'])[:20]}")
        for line in r.get("reasoning", []):
            print(f"      - {_A(line)[:110]}")
    print(f"\nFALSE NEGATIVES (you applied, scoring would reject): {len(fn)}")
    for r in fn:
        print(f"  row {r['row']} score={r['score']} {_A(r['title'])[:45]} @ {_A(r['company'])[:20]}  blockers={[_A(b)[:40] for b in r['blockers']][:3]}")
        for line in r.get("reasoning", []):
            print(f"      - {_A(line)[:110]}")
    thin = [r for r in results if sum(1 for q in r["requirements"] if q.get("met") is not None and q["level"] == "must_have") < 3]
    print(f"\nLOW-CONFIDENCE (<3 checkable must-haves extracted): {len(thin)} of {len(results)}")
    for label, grp in (("nr rows", neg), ("applied rows", pos)):
        gaps = Counter(q["skill"].lower()[:50] for r in grp for q in r["requirements"]
                       if q["level"] == "must_have" and q.get("met") is False)
        print(f"\nMost common UNMET must-haves, {label}:")
        for skill, n in gaps.most_common(12):
            print(f"  {n:>2}x {_A(skill)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.report_only:
        with open(RESULTS_PATH, encoding="utf-8") as f:
            report(json.load(f))
    else:
        report(score_all(a.limit))
