# Local Mail Agent

A local pipeline that scans Gmail for job-related email, classifies and scores each
message against a CV using a local LLM (Ollama), and keeps a Google Sheet job
tracker accurate — new opportunities appended with a real fetched description and
fit score, status changes (applied → interview → offer/reject) reflected on
existing rows with a full history trail, and stale postings grayed out
automatically. Runs unattended three times a day via Windows Task Scheduler.

This document is written to double as a design writeup: what each piece does, why
it exists, and which real bugs shaped the current design — useful both as
onboarding and as interview material.

## Contents

- [Architecture](#architecture)
- [Design decisions and the bugs behind them](#design-decisions-and-the-bugs-behind-them)
- [Testing](#testing)
- [Setup](#setup)
- [Automation / scheduling](#automation--scheduling)
- [Known limitations](#known-limitations)

## Architecture

### The three pipelines

Three independent scripts, chained in `run_mail_agent.bat`, each idempotent and
safe to run alone:

```
review_closed_positions.py  →  main.py  →  backfill_career_links.py
   (gray out stale rows)       (scan mail)   (fill in gaps)
```

1. **`review_closed_positions.py`** — re-checks every tracked, still-open row's
   link. If the posting now shows a "no longer accepting applications" banner (or,
   for a company-listings-page link, the title has simply dropped off the current
   listings), the row's status is set to `closed` and its text grayed out — never
   deleted. Runs *first* in the schedule so `main.py`'s dedup always works against
   a freshly-cleaned active set.
2. **`main.py`** — the core mail scan. Fetches unread/new mail from the Gmail
   `Work` label, classifies each message, dedups against tracked rows, scores CV
   fit via Ollama, and appends new rows or updates existing ones.
3. **`backfill_career_links.py`** — retrofits rows that ended up with no
   `url`/`description` (e.g. an application-reply email arrived with no original
   job-opportunity email ever seen). Looks up the company's career page in
   `tracked_companies.csv`, finds the specific position on it, and fills the gap.
   Gives up (marks `nr`) after `MAX_RESOLUTION_ATTEMPTS` failed tries rather than
   retrying forever.

### Module map

| Module | Responsibility |
|---|---|
| `gmail_client.py` | Gmail OAuth, fetch mail from the `Work` label, parse MIME into a plain-text `EmailMessage` (with its *real* received date, not the run date). |
| `sheets_client.py` | Sheets OAuth, row read/upsert, and most of the tracker's business rules: dedup lookups, company/title normalization, status-history formatting, the basic-filter refresh. |
| `classifier.py` | All non-LLM (regex/deterministic) email parsing — LinkedIn digest splitting, junior/intern and location filtering, workmode-suffix stripping — plus the one LLM call for triaging a single email (category/company/title/status). |
| `cv_matcher.py` | Loads/caches a parsed CV profile, and scores a job's real fetched description against it via Ollama. Enforces the hard-requirement score cap in code, not left to the LLM's own arithmetic. |
| `job_page_fetcher.py` | Plain-HTTP fetching: LinkedIn posting retrieval (canonical-URL retry logic), a generic fetch for any other URL, closed-posting detection, and DuckDuckGo search (best-effort, often blocked — see [Known limitations](#known-limitations)). |
| `company_directory.py` | Self-healing `tracked_companies.csv` — checks the CSV before ever searching, verifies a cached link is still live, and remembers a failed search so it's never silently retried every run. |
| `position_resolver.py` | Ties the above together: given a company/title (and optionally an email body), makes a real effort to find a verified link + description + confirmed company name. Never fabricates — returns blank fields if nothing checks out. |
| `notifier.py` | Windows toast notifications — new matches, and anything that needs manual attention. |
| `state.py` | Local JSON/CSV state: processed-message dedup, and a recoverable log of scored-but-rejected candidates. |
| `main.py` | Orchestrates the mail-scan pipeline: triage → dedup → resolve → score → write. |
| `review_closed_positions.py`, `backfill_career_links.py` | The other two pipeline scripts (see above). |
| `morning_flag.py` | Dedup flag so the "morning" scheduled task only actually runs once per day even if Task Scheduler's sleep-catchup and a normal firing both land the same morning. |

### Data flow for one new email

```
Gmail (Work label)
  → gmail_client.fetch_recent()        real received date, not run date
  → classifier.parse_linkedin_digest() structural (regex) split, if digest-shaped
      ├─ already-tracked job_id + subject names this posting's company?
      │     → routed as a STATUS UPDATE, not a new opportunity (see below)
      └─ else → classifier.classify_email()  (one Ollama call: category/company/title/...)
  → dedup: job_id_for() → sheets_client.find_row_by_job_id / _and_title / _and_description
  → position_resolver.resolve_position()   only path allowed to produce a description
  → cv_matcher.score_job_email()           scored against the VERIFIED description only
  → sheets_client.append_row() / update_row_fields()
  → notifier.notify_new_match()
```

## Design decisions and the bugs behind them

The interesting parts of this project are less "call an LLM" and more the
guardrails built up after each real failure mode. In rough chronological order:

**Never score without a real, verified description.**
Early on, the digest path would occasionally have no fetchable description and
still get scored against an assumed "typical" requirement set — fabricating a
gap that was never actually stated. Fixed by making the description a hard
prerequisite: no real fetched text, no score, no row (for the digest path this
was there from early on; the non-digest path only got the same discipline once
`position_resolver` existed — see "resolve, then score" below).

**Dedup is layered, not a single key** (`main.py: job_id_for`).
Priority order: (1) the employer's own requisition ID extracted from real
description text — survives a listing closing and reopening under a new
LinkedIn ID; (2) LinkedIn's own numeric listing ID; (3) exact company+title
match; (4) exact-description match for the same company. Layer 4 exists because
LinkedIn once syndicated the *same* Amazon posting through two channels (a
job-alert digest and a "jobs qualification board" recommendation), assigning two
different listing IDs to byte-identical content with no requisition ID stated —
none of the first three layers caught it. Layer 4 is deliberately gated to only
apply when **no** requisition ID exists at all: a real requisition ID is always
trusted over description similarity, so two genuinely distinct openings sharing
a boilerplate-identical template are never wrongly merged.

**LinkedIn's own "application sent" emails share the exact digest body shape.**
`Title\nCompany\nLocation\nView job: <url>` is also how LinkedIn formats its
"your application was sent to X" confirmation — meaning it would get parsed as
a *new* posting and then silently swallowed by dedup (same job_id, already
tracked) without ever updating the row's status. Fixed generically, not with a
subject-keyword hack: when a digest-shaped posting's job_id is already tracked
**and** its company is named in the subject, it's routed through the normal
triage+status pipeline instead of the new-opportunity path. (The subject-check
matters because these emails often have "you might also like" recommendations
appended below the real confirmation, sharing the same body shape — without it,
the whole email's dominant "application was sent" content would get
misattributed to those unrelated recommended postings too.)

**Status-update row matching is tiered, and the ambiguous tier is never guessed.**
`_resolve_reply_target_row` tries an exact job_id, then a title match among a
company's tracked rows, then falls back to "the only row this company has" (safe
by elimination). If a company has *multiple* tracked rows and no title match,
it used to silently apply to the first one — this is exactly what wrote an
"applied" status to the wrong Mobileye row (of three) once, and — combined with a
company-name mismatch ("Micron" vs "Micron Technology" never matching
under exact-string comparison) — created a stray duplicate row for a real
Micron application. Now that case returns "ambiguous" instead of guessing: the
email stays unread, a toast fires, and nothing gets written. Company-name
matching itself was also made more forgiving (`normalize_company` strips common
corporate suffixes) so `"Micron"` and `"Micron Technology"` are recognized as the
same company without ever risking a false merge (`"Meta"` vs `"Metadata Inc"`
stays distinct).

**A hard requirement caps the score in code, never left to the LLM's own math.**
The scoring prompt asks the LLM to separate a *sole, non-substitutable* required
skill ("strong Java expertise required") from an any-of-several-acceptable
requirement ("a language like C++, Python, or Java") into a
`hard_requirement_gaps` field. `cv_matcher._apply_hard_requirement_cap` then
caps the score in Python if that list is non-empty — the model picks out *which*
gap is real, but the arithmetic consequence is enforced deterministically rather
than trusted to whatever number the model produces.

**Resolve, then score — never against raw email text.**
The non-digest path used to score against the raw email body directly, then
separately try to find a clean description for storage — meaning a row could
end up with a real `fit_score` sitting next to a blank `description`, with no
way to see what the score had actually judged. Fixed by reordering: resolve a
real, verified description *first*; if none can be found, the email produces a
blank placeholder row (see next point) instead of a score.

**A resolution failure produces a placeholder, not nothing.**
Silently dropping an email that couldn't be resolved would lose real
opportunities whenever `position_resolver` can't verify a description (blocked
search, JS-rendered career page). Instead it appends a blank-but-visible row
with an attempt counter in `notes` (`"... (attempt 1/3)"`), which
`backfill_career_links.py` retries on every scheduled run — capped at
`MAX_RESOLUTION_ATTEMPTS` (default 3), after which it's marked `nr` instead of
retried forever.

**`tracked_companies.csv` is self-healing, and search is a one-time cost.**
`company_directory.get_career_link` checks the CSV first, verifies a cached link
is still live (re-discovering it once if not), and — critically — remembers a
*failed* discovery via a `"no link found (auto)"` marker so a company that can't
be found is never silently re-searched on every subsequent run. The marker
clears itself automatically the moment a real link is added. A first-time
failure fires a one-off desktop alert asking for the link to be added manually.

**Status changes never lose their own history.**
Overwriting `status` in place meant losing track of *when* each stage happened —
useful information for exactly the kind of retrospective an interview or
performance review would want. `sheets_client.append_status_history` appends a
`"status-date"` entry to a `History:` segment in `notes` on every real
transition, seeded at row creation. A second/third interview round is logged
even though the status text itself doesn't change (`next_status` always returns
`"interview"` regardless of prior status) — the guard is "new information
arrived", not "the status string changed".

**Row deletion was replaced with `"nr"` after a bad debugging experience.**
Deleting a duplicate row shifts every row number below it, silently invalidating
every row-number reference in past logs. This cost real time once: a correctly-
logged status update against "row 72" became forensically confusing after an
unrelated later deletion shifted numbering, briefly looking like a wrong-row bug
that had never actually happened. Standing rule since: mark duplicates `nr` with
an explanatory note, never delete.

**OAuth failures now announce themselves.**
Google's "Testing" publish status expires refresh tokens after 7 days — this
silently broke the whole pipeline once, and the gap went unnoticed for days.
`gmail_client`/`sheets_client` now catch `RefreshError` at the source and fire a
desktop notification before re-raising.

**Rejected candidates are recoverable, not just logged to a file that rotates away.**
A `[LOW FIT]` skip (including a hard-requirement-capped one) is appended to
`data/skipped_candidates.csv` — company/title/reason/score/url/summary. If a
wrong LLM judgment silently excluded a genuinely good role, it's recoverable by
inspecting that file instead of needing to already suspect something and dig
through a specific day's rotated log.

## Testing

```bash
python -m pytest tests/
```

83 tests covering the deterministic/pure logic — no network, no Gmail/Sheets
API, no Ollama calls. Each test file targets one module; most tests are
written directly against a real bug found this week rather than a generic
happy-path case, e.g.:

| File | Covers |
|---|---|
| `test_sheets_client.py` | Company/title normalization, dedup lookups (job_id, title-disambiguation, exact-description), status predicates, the attempt counter, status-history formatting |
| `test_classifier.py` | Workmode-suffix stripping, junior/intern and location filters, LinkedIn digest parsing (including the "application confirmation" body-shape edge case) |
| `test_job_page_fetcher.py` | Closed-posting detection, script/style HTML stripping, the exact-phrase snippet match (and its single-word false-positive guard) |
| `test_main.py` | `job_id_for`'s key-priority rules, `next_status` transitions, the ambiguous-vs-unambiguous row-matching tiers |
| `test_cv_matcher.py` | The code-enforced hard-requirement score cap |
| `test_company_directory.py` | CSV read/write, and — the highest-value case — that a failed search is genuinely never retried more than once |

`pytest.ini` redirects pytest's temp directory to a local `.pytest_tmp/` — this
machine's default `%TEMP%` had a permissions issue that broke `tmp_path`
fixtures otherwise.

## Setup

### Prerequisites

- Windows with Python 3.11+ on PATH
- [Ollama](https://ollama.com/) installed and running locally, with the model pulled:
  ```bash
  ollama pull qwen2.5:7b
  ```
- A CV in plain text: put it at `cv.txt` in the repo root (gitignored), or set
  `CV_TEXT_PATH` to point anywhere else
- A Google account with a Sheet already created to act as the job tracker

### Google Cloud project setup

No service account / domain-wide setup needed — this uses the installed-app
OAuth flow, authorized once via a browser sign-in.

1. **Create a project** at [console.cloud.google.com](https://console.cloud.google.com/)
2. **Enable APIs** (APIs & Services → Library): **Gmail API**, **Google Sheets API**
3. **Configure the OAuth consent screen**: User type **External** (stays in
   "Testing" is fine — see the caveat in [Known limitations](#known-limitations)
   about the 7-day token expiry this causes), add your own account under
   **Test users**
4. **Create OAuth client credentials**: Credentials → Create Credentials →
   OAuth client ID → Application type **Desktop app** → download the JSON
5. **Place the credentials file** at `credentials/oauth_client_secret.json`
   (path fixed in `config.py`'s `OAUTH_CLIENT_SECRET_PATH`)

### Google Sheet setup

1. Create a Sheet with a tab named `Jobs` (or update `SHEET_TAB` in `config.py`)
2. Row 1 headers matching `SHEET_COLUMNS` in `config.py` (columns A–S): `url,
   title, company, location, date_saved, status, description, requirements,
   cv_suggestions, cover_letter, training_project, apply_date, recruiter_name,
   recruiter_phone, recruiter_email, notes, job_id, contact_name, fit_score`
3. Copy the Sheet ID from its URL and set it via the `JOBACE_SHEET_ID` env var
   or `config.py`'s `SHEET_ID`

### Gmail label setup

Create a Gmail label called `Work` (or change `WORK_LABEL_NAME` in `config.py`).
This repo assumes a separate Gmail Apps Script tags job-related mail into this
label every ~15 min (keeps each run's query small) — a manual filter works too.

### Python environment

```bash
pip install -r requirements.txt
```

### First run (authorization)

```bash
python main.py --dry-run
```

Two browser consent windows will open (Gmail, then Sheets) — sign in and
approve each (you'll see an "unverified app" warning; this is expected for a
personal-use app in Testing mode — click Advanced → Go to \[app name\]).
Tokens cache to `data/token_gmail.json` / `data/token_sheets.json`.
`--dry-run` writes nothing, safe for this first check.

Once that succeeds:

```bash
python main.py
```

or double-click [run_mail_agent.bat](run_mail_agent.bat) (runs
`review_closed_positions.py` → `main.py` → `validate_sheet.py`;
`backfill_career_links.py` is currently commented out there pending a fix to
its career-site title-matching accuracy).

### Company career-page scanning (optional)

`scan_career_pages.py` (and `run_morning.bat`) proactively scans career pages
listed in `tracked_companies.csv` instead of waiting for email. Copy
[tracked_companies.example.csv](tracked_companies.example.csv) to
`tracked_companies.csv` (gitignored) and list your own target companies —
columns are `Company Name, Notes, Link`. Skip this entirely if you only want
email-based tracking.

### Commute / location filter

`classifier.is_location_excluded` filters out postings outside your commute
range using `config.ALLOWED_LOCATION_KEYWORDS` / `EXCLUDED_LOCATION_KEYWORDS`
— shipped with this project's original Haifa-area example values. Replace
both sets with your own city names, or empty `EXCLUDED_LOCATION_KEYWORDS` to
disable location filtering entirely.

### Configuration reference (`config.py`)

| Setting | Purpose |
|---|---|
| `SHEET_ID` / `JOBACE_SHEET_ID` env var | Target Google Sheet |
| `SHEET_TAB` | Tab name inside the sheet |
| `WORK_LABEL_NAME` | Gmail label the agent scans |
| `MAIL_LOOKBACK_DAYS` | How far back each run's Gmail query looks |
| `FIT_SCORE_THRESHOLD` | Minimum CV-fit score to surface a job |
| `HARD_REQUIREMENT_SCORE_CAP` | Score ceiling when a hard, unmet requirement is found |
| `MAX_RESOLUTION_ATTEMPTS` | Backfill retries before giving up (marks `nr`) |
| `CV_TEXT_PATH` | Path to your plain-text CV |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | Local LLM used for classification/scoring |
| `TRACKED_COMPANIES_CSV` | Self-healing company → career-page directory |

## Automation / scheduling

Three Windows Scheduled Tasks (`LocalMailAgent_Morning`, `_Noon`, `_Evening`) at
8am/12pm/5pm, each with `StartWhenAvailable` set — if the PC is asleep at the
scheduled time, Windows runs it as soon as it's next awake rather than skipping
it or forcing a wake. The morning task additionally runs through
`morning_flag.py`, a same-day dedup flag (`data/last_morning_run.txt`) so a
sleep-catchup firing landing close to the normal 8am trigger doesn't run the
full pipeline twice in one morning. Noon/evening have no such flag — they're
single fixed-time daily triggers with no double-fire risk.

```powershell
Get-ScheduledTask -TaskName "LocalMailAgent_*" | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName "LocalMailAgent_Morning" | Select-Object LastRunTime, LastTaskResult
```

## Known limitations

- **DuckDuckGo scraping (`job_page_fetcher.search_duckduckgo`) is frequently
  CAPTCHA-blocked** from this network. `company_directory` treats this as
  expected and never retries a failed search automatically (see above) — new
  companies typically need their career-page link added to
  `tracked_companies.csv` manually.
- **Most modern career pages are JS-rendered** (Comeet, Greenhouse, Ashby, many
  React sites) and return an empty template shell to a plain HTTP fetch —
  `job_page_fetcher` has no headless browser, so these need manual resolution
  (see the `resolve-missing-job-link` skill in `.claude/skills/`).
- **Indeed blocks direct scraping outright** — even a clean canonical
  `viewjob?jk=...` URL returns 401 Unauthorized, unlike LinkedIn which serves
  public postings unauthenticated.
- **OAuth refresh tokens expire after 7 days** while the Cloud project's
  consent screen is in "Testing" publish status — re-run any script
  interactively to reauthorize when this happens (now announced via a desktop
  notification instead of failing silently).
