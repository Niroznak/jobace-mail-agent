---
name: resolve-missing-job-link
description: Use when a local_mail_agent tracked row has no url/description (backfill_career_links.py logged [NO MATCH]), or company_directory.py alerted [NEEDS CSV ENTRY] for a company. Manually resolves the real posting link/description via web search + browser, since the automated DuckDuckGo-scraping fallback is CAPTCHA-blocked and most modern career pages are JS-rendered (unreachable by the script's plain HTTP fetch).
---

# Resolve a missing job link/description

The automated pipeline (`company_directory.py` + `position_resolver.py` +
`backfill_career_links.py`) can only get this far on its own:
- Check `tracked_companies.csv` for a known career-page link.
- If missing, try a DuckDuckGo scrape to discover one — **this is CAPTCHA-blocked
  from this network** and will almost always fail silently (logged as
  `[NEEDS CSV ENTRY]` and cached so it's never retried).
- If a link exists, fetch it with plain HTTP and try to match the title —
  **most modern ATS career pages (Comeet, Greenhouse, Ashby, many React sites) are
  JS-rendered** and return an empty template shell to a non-browser fetch. Only
  fully static/server-rendered pages work automatically.

When either of those fails, this skill picks up where the script stopped, using
tools the script doesn't have: web search and a real (JS-executing) browser.

## Tools

- **Read/write `tracked_companies.csv` only via its CLI** — never grep or hand-parse
  the file (its `Link` column can legitimately contain embedded commas, quoted; the
  CLI's underlying `csv.DictReader` handles that correctly, a raw grep won't):
  ```
  python company_directory.py find "<company>"              # -> JSON row, or null
  python company_directory.py list                          # -> all rows as JSON
  python company_directory.py update-link "<company>" "<url>"
  python company_directory.py update-notes "<company>" "<notes>"
  ```
- **Score a resolved posting** — never invent a fit_score:
  `python -c "import cv_matcher; print(cv_matcher.score_job_email('<company>', '<title>', '''<description>'''))"`
- **Write the sheet**:
  `python -c "import sheets_client; s = sheets_client.get_sheets_service(); sheets_client.update_row_fields(s, <row>, {...})"`

## Procedure

1. Identify the row(s) needing resolution — a `backfill_career_links.py`
   `[NO MATCH]` log line, a `company_directory` `[NEEDS CSV ENTRY]` alert, or the
   user pointing at a specific row/company/title.

2. Check the CSV: `python company_directory.py find "<company>"`. If it already has
   a `Link`, try fetching it yourself first — it may just need JS rendering the
   script's plain HTTP fetch can't do.

3. If no usable link exists, web-search `"<company> careers <title>"` (or just
   `"<company> careers"` if title is blank/generic). Prefer the company's own domain
   or a company-specific ATS page (Comeet, Greenhouse, Lever, Workday). Avoid
   aggregators (LinkedIn, Indeed, Glassdoor, ZipRecruiter...) as the *stored* link —
   use them only to find the real posting, then navigate to the company's own page.

4. Verify with your browser (`navigate` + `get_page_text`, not a raw fetch) — confirm
   real listings render, not a template shell (`{{...}}` placeholders or near-empty
   nav-only text means JS didn't finish, or listings are paginated/behind a widget).

5. Find the specific position (`find`/`javascript_exec` on the rendered page), then
   navigate to its direct posting page and `get_page_text` the full description. If
   the exact title isn't listed, don't force a match — report it as likely
   closed/filled and leave the row untouched.

6. Score it (see Tools above) against the real fetched description.

7. Write the results:
   - Sheet: only set the fields you actually resolved (`url`/`description`/
     `fit_score`/occasionally a corrected `company`) — never touch `status`,
     `date_saved`, or `apply_date`.
   - CSV: `update-link`/`update-notes` if the company was missing or its link was
     wrong/dead — this is what makes future rows for the same company resolve
     automatically without needing this skill again.

## Guardrails (carried over from the codebase's own "never invent" principle)

- Never store a raw email dump, a company's whole multi-job listing page, or
  aggregator-site boilerplate as `description` — only the specific position's real
  text.
- Never guess a `fit_score` — always run it through `cv_matcher.score_job_email`
  against real fetched content.
- If nothing can be verified, leave the row exactly as-is and say so — a gap is
  honest; a wrong guess isn't.
- Don't touch `status`, `date_saved`, or `apply_date` — this skill only fills
  `url`/`description`/`fit_score`/(occasionally a corrected `company` name).

## If resolution reveals a duplicate: mark "nr", never delete the row

While resolving a gap (or during any other cleanup), you may discover a row is an
exact duplicate of another (same job_id, or byte-identical description). **Never
use `sheets_client.delete_rows()` for this.** Deleting a row shifts every row number
below it, which silently invalidates every row-number reference in past logs and
notes — this cost real debugging time once already (a status update correctly
logged against "row 72" became forensically confusing after an unrelated later
deletion shifted numbering, making it look like a wrong-row bug that never actually
happened).

Instead: set the duplicate's `status` to `"nr"` and its `notes` to something like
`"Duplicate of row <N> (identical job_id/description)."`. This achieves the same
practical outcome (the sheet's existing filter already hides `nr` rows from view)
without ever renumbering anything. Reserve actual deletion for the user explicitly
asking to prune the sheet, never as a routine dedup-cleanup step.
