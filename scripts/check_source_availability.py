"""Standalone, on-demand capability check: what can the agent actually fetch right
now? Run this BEFORE a batch run when you want to know up front whether LinkedIn/
Indeed/discovery/a given company's site are reachable, instead of finding out by
watching the same failure repeat across many candidates.

Report-only -- makes no writes anywhere (not to the sheet, not to any state file).

Sources checked, and why each uses a different method (see the plan this came from):
  - linkedin : active probe against a generic, stable LinkedIn jobs page (not one
               specific posting, which can legitimately be gone on its own).
  - indeed   : Indeed's main site being up says nothing about whether its
               click-tracking links (the actual thing we fetch) are blocked -- that's
               separate infrastructure. Reuses a REAL recent tracking URL from
               today's log or data/dropped_verification.csv rather than fabricating
               one. Reports "untested" if no recent one exists -- never guesses.
  - discovery: SerpAPI's free tier is 100 queries/MONTH (see config.py) -- an active
               probe here would burn scarce quota just to run a diagnostic. Reported
               passively: is a key configured, and when did career-page discovery
               last actually succeed (derived from tracked_companies.csv).
  - company career sites: not one source but dozens of independent ones:
               tracked_companies.csv rows. Each is fetched and classified
               independently via the exact same extract_job_links scan_career_pages.py
               already trusts.

New source to add later = one new entry in _PLATFORM_SOURCES, not new logic.

Usage:
    python check_source_availability.py                 # full report
    python check_source_availability.py --skip-companies # platform/discovery only
"""
from __future__ import annotations

import argparse
import csv
import glob
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import company_directory, config, job_page_fetcher

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.WARNING, format="%(message)s")  # quiet -- this tool IS the output
logger = logging.getLogger(__name__)

_A = lambda s: str(s).encode("ascii", "replace").decode()


# --- Part 1: shared-platform sources (data-driven; add a new one as one more entry) ---

@dataclass
class PlatformSource:
    name: str
    check_kind: str  # "active_probe" | "recent_real_url"
    probe_url: str = ""  # for active_probe


_PLATFORM_SOURCES = [
    PlatformSource("linkedin", "active_probe", probe_url="https://www.linkedin.com/jobs/search/?keywords=engineer"),
    PlatformSource("indeed", "recent_real_url"),
]


def _find_recent_real_url(platform: str, max_age_hours: int = 48) -> str | None:
    """A real URL for this platform seen recently in logs or the dropped-verification
    CSV -- never fabricated. Returns None (reported as "untested") if nothing recent
    is on record, rather than guessing with a made-up URL that would test nothing."""
    cutoff = time.time() - max_age_hours * 3600
    candidates: list[tuple[float, str]] = []

    if os.path.exists(config.DROPPED_VERIFICATION_PATH):
        with open(config.DROPPED_VERIFICATION_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                url = row.get("url", "")
                if url and job_page_fetcher.classify_platform(url) == platform:
                    try:
                        ts = datetime.strptime(row["date"], "%Y-%m-%d").timestamp()
                    except (KeyError, ValueError):
                        continue
                    candidates.append((ts, url))

    url_re = re.compile(r"https?://\S+")
    for log_path in sorted(glob.glob(os.path.join(config.LOGS_DIR, "agent_*.log")), reverse=True)[:2]:
        mtime = os.path.getmtime(log_path)
        if mtime < cutoff:
            continue
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "fetching" not in line and "url=" not in line.lower():
                    continue
                for match in url_re.finditer(line):
                    url = match.group(0).rstrip(").,>\"'")
                    if job_page_fetcher.classify_platform(url) == platform:
                        candidates.append((mtime, url))

    fresh = [(ts, u) for ts, u in candidates if ts >= cutoff]
    if not fresh:
        return None
    return max(fresh, key=lambda pair: pair[0])[1]


def check_platform(source: PlatformSource) -> dict:
    if source.check_kind == "active_probe":
        html = job_page_fetcher.fetch_raw_html(source.probe_url)
        status = "OK" if html else "BLOCKED"
        detail = source.probe_url if html else f"fetch failed on {source.probe_url} (see warning above)"
        return {"name": source.name, "status": status, "detail": detail}

    if source.check_kind == "recent_real_url":
        url = _find_recent_real_url(source.name)
        if not url:
            return {"name": source.name, "status": "UNTESTED", "detail": "no real URL from the last 48h to test against"}
        html = job_page_fetcher.fetch_raw_html(url)
        status = "OK" if html else "BLOCKED"
        detail = f"tested against a real URL from recent history: {job_page_fetcher.short_url(url)}"
        return {"name": source.name, "status": status, "detail": detail}

    raise ValueError(f"Unknown check_kind: {source.check_kind}")


# --- Part 2: discovery (SerpAPI/DuckDuckGo) -- passive, never burns quota ---

def check_discovery() -> dict:
    key_configured = bool(config.SERPAPI_API_KEY)
    rows = company_directory.load()
    discovered = [
        r for r in rows
        if r.get("Link", "").strip() and company_directory.NO_LINK_FOUND_MARKER not in r.get("Notes", "").lower()
    ]
    last = discovered[-1]["Company Name"] if discovered else None
    status = "OK" if key_configured else "NOT CONFIGURED"
    detail = f"SerpAPI key {'configured' if key_configured else 'missing (SERPAPI_API_KEY)'}"
    if last:
        detail += f"; most recently used for '{last}' ({len(discovered)} compan(y/ies) with a discovered link total)"
    return {"name": "discovery", "status": status, "detail": detail}


# --- Part 3: company career sites -- one independent check per tracked_companies.csv row ---

def check_company_sites() -> dict:
    rows = company_directory.load()
    scannable = [
        r for r in rows
        if r.get("Link", "").strip()
        and "disqualified" not in r.get("Notes", "").lower()
        and company_directory.NO_LINK_FOUND_MARKER not in r.get("Notes", "").lower()
    ]
    reachable_with_jobs, reachable_no_jobs, unreachable, errored = [], [], [], []
    for row in scannable:
        company, link = row["Company Name"].strip(), row["Link"].strip()
        time.sleep(2)  # same politeness pacing as scan_career_pages.py
        try:
            html = job_page_fetcher.fetch_raw_html(link)
        except Exception as exc:
            errored.append((company, f"{exc.__class__.__name__}"))
            continue
        if not html:
            unreachable.append(company)
            continue
        links = job_page_fetcher.extract_job_links(html, link)
        (reachable_with_jobs if links else reachable_no_jobs).append(company)
    return {
        "total": len(scannable),
        "reachable_with_jobs": reachable_with_jobs,
        "reachable_no_jobs": reachable_no_jobs,
        "unreachable": unreachable,
        "errored": errored,
    }


def main(skip_companies: bool) -> None:
    print("[SOURCE STATUS]")
    for source in _PLATFORM_SOURCES:
        result = check_platform(source)
        print(f"  {result['name']:10} : {result['status']:10} ({result['detail']})")
    disc = check_discovery()
    print(f"  {disc['name']:10} : {disc['status']:10} ({disc['detail']})")

    if skip_companies:
        return

    print(f"\n[COMPANY CAREER SITES]")
    c = check_company_sites()
    print(f"  tracked with a usable link : {c['total']}")
    print(f"  reachable with job links   : {len(c['reachable_with_jobs'])}")
    print(f"  reachable, no postings now : {len(c['reachable_no_jobs'])}" + (f"  ({', '.join(_A(n) for n in c['reachable_no_jobs'][:5])})" if c['reachable_no_jobs'] else ""))
    print(f"  unreachable/JS-rendered    : {len(c['unreachable'])}" + (f"  ({', '.join(_A(n) for n in c['unreachable'][:5])}{'...' if len(c['unreachable']) > 5 else ''})" if c['unreachable'] else ""))
    print(f"  fetch error                : {len(c['errored'])}" + (f"  ({', '.join(_A(n) + ':' + e for n, e in c['errored'])})" if c['errored'] else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-companies", action="store_true", help="Platform/discovery status only, skip the per-company sweep")
    args = ap.parse_args()
    main(args.skip_companies)
