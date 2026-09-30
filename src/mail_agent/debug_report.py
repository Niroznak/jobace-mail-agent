"""Per-run debug reporting: captures exactly what each candidate went through and why,
without needing to grep the day's full log file for one item's story.

Real motivation: "the agent doesn't seem to work properly, I don't see any update" --
with no per-run structured summary, the only way to see WHY a given run produced no
sheet changes was scrolling a log file full of multi-hundred-character tracking URLs.
LogCapture piggybacks on the logging every pipeline stage already does (VERIFY/SCORE/
RECONCILE lines) instead of changing every stage's return type to carry a reason string.
"""
from __future__ import annotations

import json
import logging


class LogCapture:
    """Context manager: captures every log message emitted anywhere in this process
    while the `with` block runs (verify/score/reconcile all log their own reasoning at
    INFO/WARNING/ERROR), so it can be attached to a candidate's debug record as-is.

    Real bug caught by this module's own tests: adding a handler alone isn't enough --
    a message below the ROOT logger's current effective level (WARNING by default
    whenever nothing has called logging.basicConfig yet, e.g. any script/test that
    doesn't set up logging first) never reaches ANY handler at all, silently dropping
    exactly the INFO-level reasoning lines (VERIFY/SCORE/RECONCILE) this exists to
    capture. The root level is temporarily lowered to DEBUG for the duration of the
    block and restored on exit, so nothing is missed regardless of ambient setup."""

    def __init__(self):
        self.lines: list[str] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.lines.append(record.getMessage())
        self._previous_level: int | None = None

    def __enter__(self) -> "LogCapture":
        root = logging.getLogger()
        self._previous_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info) -> None:
        root = logging.getLogger()
        root.removeHandler(self._handler)
        root.setLevel(self._previous_level)


def write_report(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


_SYSTEMIC_MIN_ATTEMPTS = 3  # below this, a 100% failure rate is just small-sample noise


def find_systemic_platform_failures(records: list[dict]) -> list[tuple[str, int, int]]:
    """Detects a platform-wide block from this run's own candidates: e.g. every single
    Indeed fetch failing is a fundamentally different situation from a few scattered
    bad links -- it means "Indeed is blocking us right now", not "these specific
    postings have issues", and deserves a distinct, louder alert (see main.py) instead
    of blending into the routine per-candidate verify_failed count.

    Returns (platform, failed_count, total_count) for each platform where every
    opportunity candidate on that platform failed verification this run, requiring at
    least _SYSTEMIC_MIN_ATTEMPTS candidates on that platform to rule out a false alarm
    from a run that only touched one or two Indeed/LinkedIn postings."""
    from . import job_page_fetcher  # deferred: keeps this module import-light for tests

    totals: dict[str, int] = {}
    failures: dict[str, int] = {}
    for r in records:
        if r.get("kind") != "opportunity" or not r.get("url"):
            continue
        platform = job_page_fetcher.classify_platform(r["url"])
        if platform == "other":
            continue
        totals[platform] = totals.get(platform, 0) + 1
        if r["action"] == "verify_failed":
            failures[platform] = failures.get(platform, 0) + 1

    return [
        (platform, failures.get(platform, 0), total)
        for platform, total in totals.items()
        if total >= _SYSTEMIC_MIN_ATTEMPTS and failures.get(platform, 0) == total
    ]


def print_summary(records: list[dict]) -> None:
    """Concise, human-readable end-of-run table -- for --debug runs, printed to the
    console in addition to the JSON file, so the answer to "what happened and why" is
    visible immediately without opening a file."""
    if not records:
        print("\n[DEBUG SUMMARY] No candidates were extracted this run.")
        return
    by_action: dict[str, list[dict]] = {}
    for r in records:
        by_action.setdefault(r["action"], []).append(r)

    print(f"\n[DEBUG SUMMARY] {len(records)} candidate(s) this run:")
    for action, group in sorted(by_action.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  {action} ({len(group)}):")
        for r in group:
            print(f"    - {r['title'][:60]!r} @ {r['company'][:30]!r}")
            for line in r["log"][-3:]:  # the last lines are usually the actual verdict
                print(f"        {line[:160]}")
