"""Self-healing directory of company -> career-page link, backed by the user-curated
tracked_companies.csv (columns: Company Name, Notes, Link).

Lookups check the CSV first; a link that's dead or doesn't even mention the company
gets repaired via a fresh web search rather than trusted blindly, and a company missing
entirely gets discovered and appended -- so the CSV improves itself over time instead of
going stale.
"""
from __future__ import annotations

import csv
import logging
import os

from . import config
from . import job_page_fetcher
from . import notifier

logger = logging.getLogger(__name__)

_FIELDNAMES = ["Company Name", "Notes", "Link"]

_AGGREGATOR_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
}


def load() -> list[dict]:
    if not os.path.exists(config.TRACKED_COMPANIES_CSV):
        return []
    with open(config.TRACKED_COMPANIES_CSV, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _save_all(rows: list[dict]) -> None:
    with open(config.TRACKED_COMPANIES_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def find(company: str) -> dict | None:
    company_lower = company.strip().lower()
    for row in load():
        if row.get("Company Name", "").strip().lower() == company_lower:
            return row
    return None


def append(company: str, notes: str, link: str) -> None:
    file_exists = os.path.exists(config.TRACKED_COMPANIES_CSV)
    with open(config.TRACKED_COMPANIES_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({"Company Name": company, "Notes": notes, "Link": link})
    logger.info("[DIRECTORY] added %s -> %r", company, link)


def update_link(company: str, new_link: str) -> None:
    rows = load()
    company_lower = company.strip().lower()
    found = False
    for row in rows:
        if row.get("Company Name", "").strip().lower() == company_lower:
            row["Link"] = new_link
            row["Notes"] = ""  # a resolved link clears any stale "no link found" marker
            found = True
            break
    if not found:
        rows.append({"Company Name": company, "Notes": "", "Link": new_link})
    _save_all(rows)
    logger.info("[DIRECTORY] updated %s -> %r", company, new_link)


def update_notes(company: str, notes: str) -> None:
    rows = load()
    company_lower = company.strip().lower()
    found = False
    for row in rows:
        if row.get("Company Name", "").strip().lower() == company_lower:
            row["Notes"] = notes
            found = True
            break
    if not found:
        rows.append({"Company Name": company, "Notes": notes, "Link": ""})
    _save_all(rows)


def is_link_usable(link: str, company: str) -> bool:
    if not link.strip():
        return False
    posting = job_page_fetcher.fetch_generic_posting(link)
    if not posting.description:
        return False
    return company.strip().lower() in posting.description.lower()


def _is_aggregator(url: str) -> bool:
    domain = url.split("/")[2].lower() if "://" in url else ""
    return any(domain == d or domain.endswith("." + d) for d in _AGGREGATOR_DOMAINS)


def _pick_from_results(results: list, company: str) -> str | None:
    company_lower = company.strip().lower()
    for r in results:
        if _is_aggregator(r.url):
            continue
        if is_link_usable(r.url, company):
            return r.url
        # Plain fetch came back empty -- likely a JS-rendered page (confirmed case:
        # career.rafael.co.il serves an empty bot-protection shell to non-browser
        # clients). The search engine's own crawler already rendered and indexed it,
        # so its title/snippet is a real, non-fabricated signal we can use instead
        # of giving up on an otherwise-correct search hit.
        if company_lower in (r.title + " " + r.snippet).lower():
            return r.url
    return None


def discover_career_link(company: str) -> str | None:
    query = f"{company} careers"

    # SerpAPI first (free tier, 100/month) -- the primary provider since Google's own
    # Custom Search "search the entire web" toggle is locked on this account.
    results = job_page_fetcher.search_serpapi_full(query)
    if results:
        return _pick_from_results(results, company)

    # Google Custom Search -- dormant unless GOOGLE_SEARCH_API_KEY/CX are ever set;
    # kept in case the entire-web toggle gets unlocked later.
    results = job_page_fetcher.search_google_custom_full(query)
    if results:
        return _pick_from_results(results, company)

    # Last resort: no-key DuckDuckGo search (best-effort, sometimes CAPTCHA-blocked).
    for url in job_page_fetcher.search_duckduckgo(query):
        if _is_aggregator(url):
            continue
        if is_link_usable(url, company):
            return url
    return None


NO_LINK_FOUND_MARKER = "no link found (auto)"


def get_career_link(company: str) -> tuple[str | None, str | None]:
    """Returns (link, notes). link is None if no usable career page could be found or
    confirmed; notes carries through free-text markers like "disqualified" or
    "no positions page" for the caller to act on.

    Search is a one-time cost per company, not a recurring dependency: a failed
    discovery attempt is remembered via the "no link found (auto)" notes marker so
    it's never silently retried on every future run (search is best-effort and can
    be blocked/unreliable) -- once a real link is known (found manually, or added to
    tracked_companies.csv), that marker clears automatically via update_link."""
    row = find(company)
    if row is not None:
        notes = row.get("Notes", "")
        link = row.get("Link", "").strip()
        if "disqualified" in notes.lower():
            return None, notes
        if link and is_link_usable(link, company):
            if NO_LINK_FOUND_MARKER in notes.lower():
                # You added the link yourself after the earlier alert -- clear the
                # stale marker so the CSV doesn't keep showing "no link found" next
                # to a Link column that now has a real, working URL.
                update_notes(company, "")
                notes = ""
            return link, notes
        if NO_LINK_FOUND_MARKER in notes.lower():
            return None, notes
        # Link is blank, dead, or irrelevant -- try to (re)discover it, once.
        discovered = discover_career_link(company)
        if discovered:
            update_link(company, discovered)
            return discovered, ""
        update_notes(company, NO_LINK_FOUND_MARKER)
        _alert_no_link_found(company)
        return None, NO_LINK_FOUND_MARKER

    discovered = discover_career_link(company)
    if discovered:
        append(company, "", discovered)
        return discovered, ""
    append(company, NO_LINK_FOUND_MARKER, "")
    _alert_no_link_found(company)
    return None, NO_LINK_FOUND_MARKER


def _alert_no_link_found(company: str) -> None:
    """Fires once, the first time a company's career link can't be auto-discovered
    (search is blocked/unreliable) -- surfaces it for a one-time manual add to
    tracked_companies.csv rather than letting it silently stay gapped forever.
    Never re-fires on subsequent runs: get_career_link returns early via the
    NO_LINK_FOUND_MARKER check before this function would be reached again."""
    logger.warning("[NEEDS CSV ENTRY] '%s' has no known career page -- add it to tracked_companies.csv manually.", company)
    notifier.notify_needs_review(f"Add career page link for '{company}' to tracked_companies.csv")


def _cli() -> None:
    """CLI so tracked_companies.csv is always read/written through this module's
    correct CSV-quoting-aware functions -- never a raw grep or hand-rolled parse of
    a file whose Link column can legitimately contain embedded commas."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Read/update tracked_companies.csv safely")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find", help="Look up one company's row")
    p_find.add_argument("company")

    p_list = sub.add_parser("list", help="List all rows")

    p_link = sub.add_parser("update-link", help="Set/repair a company's career-page link")
    p_link.add_argument("company")
    p_link.add_argument("link")

    p_notes = sub.add_parser("update-notes", help="Set a company's notes (e.g. 'disqualified')")
    p_notes.add_argument("company")
    p_notes.add_argument("notes")

    args = parser.parse_args()

    if args.cmd == "find":
        row = find(args.company)
        print(json.dumps(row, ensure_ascii=False, indent=2) if row else "null")
    elif args.cmd == "list":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    elif args.cmd == "update-link":
        update_link(args.company, args.link)
    elif args.cmd == "update-notes":
        update_notes(args.company, args.notes)


if __name__ == "__main__":
    _cli()
