"""Tracks whether the morning automated run has already completed today.

Used by run_morning.bat so a repeated wake/unlock trigger -- or Task Scheduler's own
"run as soon as possible after a missed start" catch-up firing close to the normal
8am trigger -- doesn't run the full pipeline twice on the same morning.
"""
from __future__ import annotations

import argparse
import os
from datetime import date

import config

FLAG_PATH = os.path.join(config.DATA_DIR, "last_morning_run.txt")


def already_ran_today() -> bool:
    if not os.path.exists(FLAG_PATH):
        return False
    with open(FLAG_PATH, "r", encoding="utf-8") as f:
        return f.read().strip() == date.today().isoformat()


def mark_ran_today() -> None:
    with open(FLAG_PATH, "w", encoding="utf-8") as f:
        f.write(date.today().isoformat())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Morning-run dedup flag")
    parser.add_argument("action", choices=["check", "mark"])
    args = parser.parse_args()

    if args.action == "check":
        raise SystemExit(0 if already_ran_today() else 1)
    mark_ran_today()
