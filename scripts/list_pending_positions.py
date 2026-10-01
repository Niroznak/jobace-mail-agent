"""Lists currently-new, title-plausible opportunity candidates whose source platform
is blocked right now AND that couldn't be resolved automatically any other way --
the genuine "what's left that needs you to open it yourself" list.

Before ever asking you to open anything, each candidate is tried against
position_resolver.find_on_career_page: if the company's own career site already
lists this exact position, it's scored and written to the sheet automatically, no
manual action needed, and it's removed from what gets shown to you.

Results are staged in data/pending_manual_links.csv (state.load/save_pending_manual_links),
keyed by job_id, so re-running this script never repeats something already shown,
auto-resolved, or determined low-fit -- only genuinely new or still-unprocessed
candidates print. Makes writes ONLY for candidates find_on_career_page resolves
automatically; everything requiring your action is report-only.

Once you've opened one of the printed links in your own Chrome, tell Claude -- it
reads the already-loaded tab (via the Claude-in-Chrome extension) and resolves it
through position_resolver.resolve_from_page_text, the same scoring/dedup/insert path
a successful automated fetch would have used.

Usage:
    python list_pending_positions.py                         # re-scan mail, print the list
    python list_pending_positions.py --html pending.html      # write already-staged
                                                                # unprocessed links as a
                                                                # clickable local page --
                                                                # no network calls, no
                                                                # re-scan. Open that file
                                                                # with Chrome once (right-
                                                                # click -> Open with ->
                                                                # Chrome) and every link
                                                                # on it opens in Chrome too
                                                                # -- a browser's own link
                                                                # clicks don't go through
                                                                # Windows' default-browser
                                                                # handler the way a non-
                                                                # browser app's (Excel's)
                                                                # hyperlinks do.
"""
from __future__ import annotations

import argparse
import html as html_module
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import company_directory, config, gmail_client, job_page_fetcher, position_resolver, sheets_client, state
from mail_agent.pipeline import dedup, extract, verify

# SerpAPI's free tier is 100 queries/MONTH (config.py's own comment) --
# find_on_career_page's company_directory.get_career_link triggers one search per
# company not already in tracked_companies.csv. A run with many new companies could
# burn a large chunk of that in one invocation; capped by default, remaining new
# companies just skip straight to the manual list instead of being discovery-tried.
_DEFAULT_MAX_NEW_DISCOVERIES = 5

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_A = lambda s: str(s).encode("ascii", "replace").decode()
_TERMINAL_STATUSES = {"resolved", "skipped_low_fit", "auto_resolved"}

# Mirrors check_source_availability.py's platform probes -- a fresh, accurate
# right-now check rather than trusting the in-process breaker, which may be cold if
# this script is run standalone rather than mid-batch.
_PROBE_URLS = {
    "linkedin": "https://www.linkedin.com/jobs/search/?keywords=engineer",
}


def _platform_blocked_now(platform: str) -> bool:
    if platform == "linkedin":
        return job_page_fetcher.fetch_raw_html(_PROBE_URLS["linkedin"]) is None
    if platform == "indeed":
        # No safe generic probe for Indeed (see check_source_availability.py's own
        # docstring: its main site being up says nothing about tracking-link
        # blocking) -- treat as blocked-by-default for this list's purpose, since
        # every real Indeed candidate's URL IS a tracking link.
        return True
    return False


def main(max_new_discoveries: int) -> None:
    gmail = gmail_client.get_gmail_service()
    sheets = sheets_client.get_sheets_service()
    sheet_rows = sheets_client.fetch_all_rows(sheets)
    staged = state.load_pending_manual_links()
    tracked_companies = {r["Company Name"].strip().lower() for r in company_directory.load()}
    new_discoveries_used = 0

    blocked_platforms = {p for p in ("linkedin", "indeed") if _platform_blocked_now(p)}
    print(f"Blocked right now: {', '.join(sorted(blocked_platforms)) or '(none)'}\n")

    all_ids = gmail_client.list_recent_ids(gmail, newer_than_days=config.MAIL_LOOKBACK_DAYS)
    messages = [gmail_client.fetch_message(gmail, i) for i in all_ids]

    auto_resolved = 0
    to_print: list[tuple[str, str, str]] = []  # (title, company, url)
    seen_job_ids: set[str] = set()

    for msg in messages:
        try:
            candidates = extract.extract_candidates(msg, sheet_rows)
        except Exception:
            continue
        for c in candidates:
            if c.kind != "opportunity" or not c.url:
                continue
            platform = job_page_fetcher.classify_platform(c.url)
            if platform not in blocked_platforms:
                continue  # this one's source is fine -- the normal automated path handles it
            if not verify._passes_early_filters(c):
                continue  # same title/location/generic-listing rules the automated path applies
            jid = dedup.job_id_for(c.company, c.title, c.position_id)
            if sheets_client.find_row_by_job_id(sheets_client.active_rows(sheet_rows), jid):
                continue  # already tracked -- verify.py's own dedup check, applied here too
            if jid in seen_job_ids:
                continue  # same posting seen in another digest email this run
            seen_job_ids.add(jid)

            staged_entry = staged.get(jid)
            if staged_entry and staged_entry.get("status") in _TERMINAL_STATUSES:
                continue  # already auto-resolved / resolved / determined low-fit previously

            # Try the automatic alternative BEFORE ever asking you to open anything --
            # but a company not already tracked costs one of the capped SerpAPI
            # searches, so skip straight to the manual list once the cap is spent
            # rather than silently burning the remaining monthly quota.
            is_new_company = c.company.strip().lower() not in tracked_companies
            if is_new_company and new_discoveries_used >= max_new_discoveries:
                state.upsert_pending_manual_link(jid, c.company, c.title, c.url, "unprocessed")
                to_print.append((c.title, c.company, c.url))
                continue
            if is_new_company:
                new_discoveries_used += 1

            resolved = position_resolver.find_on_career_page(c.company, c.title)
            if resolved.url:
                result = position_resolver.resolve_from_page_text(
                    resolved.company, c.title, resolved.url, resolved.description, sheet_rows, sheets, dry_run=False,
                )
                status = "auto_resolved" if result.action in ("inserted", "updated") else "skipped_low_fit"
                state.upsert_pending_manual_link(jid, resolved.company, c.title, resolved.url, status)
                if result.action in ("inserted", "updated"):
                    auto_resolved += 1
                    print(f"[AUTO-RESOLVED] {_A(c.title)} @ {_A(resolved.company)} -> {result.action} (row {result.row_number})")
                continue

            state.upsert_pending_manual_link(jid, c.company, c.title, c.url, "unprocessed")
            to_print.append((c.title, c.company, c.url))

    # Include anything still "unprocessed" from a previous run too (e.g. its
    # triggering email already scrolled out of MAIL_LOOKBACK_DAYS) -- the staging
    # file is the source of truth for what's still outstanding, not just this run's
    # fresh extraction.
    fresh_urls = {url for _, _, url in to_print}
    for entry in staged.values():
        if entry.get("status") == "unprocessed" and entry.get("url") not in fresh_urls:
            to_print.append((entry.get("title", ""), entry.get("company", ""), entry.get("url", "")))

    if auto_resolved:
        print(f"\n{auto_resolved} position(s) auto-resolved via the company's own career page -- no action needed.\n")

    print(f"[NEEDS YOU TO OPEN] {len(to_print)} position(s):\n")
    for i, (title, company, url) in enumerate(to_print, 1):
        print(f"{i}. {_A(title)} @ {_A(company)}")
        print(f"   {url}\n")
    if not to_print:
        print("Nothing left that needs manual action right now.")


def write_html_list(out_path: str) -> int:
    """Writes every still-"unprocessed" staged entry as a clickable local HTML page --
    purely a reformat of data/pending_manual_links.csv, no network calls, no re-scan.
    Open the result with Chrome once (right-click -> Open with -> Chrome); every link
    clicked from inside that page then opens in Chrome too, with no default-browser
    change needed -- a browser's own link navigation doesn't go through Windows'
    default-browser handler the way a non-browser app's (Excel's) hyperlinks do."""
    staged = state.load_pending_manual_links()
    unprocessed = [r for r in staged.values() if r.get("status") == "unprocessed"]
    rows_html = "\n".join(
        f'<tr><td>{i}</td><td>{html_module.escape(r.get("title",""))}</td>'
        f'<td>{html_module.escape(r.get("company",""))}</td>'
        f'<td><a href="{html_module.escape(r.get("url",""), quote=True)}" target="_blank" rel="noopener">open</a></td></tr>'
        for i, r in enumerate(unprocessed, 1)
    )
    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pending positions</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
a {{ color: #1a73e8; }}
</style></head>
<body>
<h2>{len(unprocessed)} position(s) needing manual action</h2>
<p>Each link opens in a new tab. Opened from here, links stay in Chrome regardless of your system default browser.</p>
<table><tr><th>#</th><th>Title</th><th>Company</th><th>Link</th></tr>
{rows_html}
</table>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return len(unprocessed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--max-new-discoveries", type=int, default=_DEFAULT_MAX_NEW_DISCOVERIES,
        help=f"Cap on SerpAPI searches for companies not yet in tracked_companies.csv (default {_DEFAULT_MAX_NEW_DISCOVERIES})",
    )
    ap.add_argument("--html", metavar="PATH", help="Write already-staged unprocessed links as a clickable HTML page instead of re-scanning mail")
    args = ap.parse_args()
    if args.html:
        count = write_html_list(args.html)
        print(f"Wrote {count} unprocessed link(s) to {args.html}")
    else:
        main(args.max_new_discoveries)
