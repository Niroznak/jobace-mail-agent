# Goal

Automate job-search tracking: actively source job postings both from Gmail (LinkedIn
digests, direct opportunity emails, application-reply confirmations) and by
proactively scanning tracked companies' career pages, score real postings against my
CV via a local LLM, and keep a Google Sheet job tracker accurate -- real
links/descriptions only, no closed/junior/out-of-range rows, no duplicates.

In scope:
- Two ingestion sources treated as equally important: email scanning and direct
  company career-page scanning (tracked_companies.csv)
- Keeping existing tracked rows correct over time: closing stale postings, backfilling
  missing links/descriptions, logging status-change history
- Reliability of the sheet itself (dedup, ambiguous-match safety, test coverage) so
  it's trustworthy without manual double-checking

Out of scope for now: anything beyond this single user's own job search (no
multi-user support, no outbound messaging/applying on the user's behalf).
