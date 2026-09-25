"""Paths, IDs, and tunables for the mail agent. Fill in SHEET_ID before first run."""
from __future__ import annotations

import os

# This file lives at <repo_root>/src/mail_agent/config.py -- data/, logs/, and
# credentials/ are repo-root-relative regardless of where the package itself is
# installed from, so BASE_DIR climbs back up two levels rather than using this
# file's own directory.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
CREDENTIALS_DIR = os.path.join(BASE_DIR, "credentials")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(CREDENTIALS_DIR, exist_ok=True)

# --- Google Sheet (jobAce tracker) ---
# Set via the JOBACE_SHEET_ID environment variable (see README) -- deliberately no
# hardcoded fallback here, since this file is committed to git and a real sheet ID
# doesn't belong in source control even though it alone can't grant access.
SHEET_ID = os.environ.get("JOBACE_SHEET_ID", "")
SHEET_TAB = "Jobs"

# Existing jobAce columns (A-P) plus new ones appended at the end (Q, R, S).
SHEET_COLUMNS = [
    "url", "title", "company", "location", "date_saved", "status",
    "description", "requirements", "cv_suggestions", "cover_letter",
    "training_project", "apply_date", "recruiter_name", "recruiter_phone",
    "recruiter_email", "notes",
    "job_id", "contact_name", "fit_score",
]
SHEET_RANGE_FULL = "A2:S"
SHEET_RANGE_HEADER = "A1:S1"

STATUS_NOT_APPLIED_YET = "not applied yet"

# Real bug found 2026-09-17: a Dell posting's "Strong knowledge of TypeScript, Node.js,
# and modern web frameworks (React/Vue)" hard requirement sat at character 3451 of a
# 5782-char real fetched description -- cv_matcher.score_job_email was truncating to
# 2000 chars internally (even though callers already truncated to 3000 beforehand),
# so the LLM never saw that requirement and couldn't cap the score for it. 5000 chars
# comfortably fits Ollama's 4096-token context alongside the ~2000-char CV profile
# JSON and prompt template (verified: well under half the token budget), while
# covering real postings' requirements sections that sit well past company-boilerplate
# intros. The stored (sheet-visible) description is bumped to match, for the same
# reason -- a truncated stored description was hiding the very requirement the user
# needed to see to sanity-check the score themselves.
DESCRIPTION_SCORE_CHARS = 5000
DESCRIPTION_STORE_CHARS = 3000

# --- OAuth ---
OAUTH_CLIENT_SECRET_PATH = os.path.join(CREDENTIALS_DIR, "oauth_client_secret.json")
TOKEN_GMAIL_PATH = os.path.join(DATA_DIR, "token_gmail.json")
TOKEN_SHEETS_PATH = os.path.join(DATA_DIR, "token_sheets.json")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --- Company directory ---
TRACKED_COMPANIES_CSV = os.path.join(BASE_DIR, "tracked_companies.csv")

# --- CV ---
# Point this at your own plain-text CV. Defaults to cv.txt at the repo root
# (gitignored -- put your real one there, or set CV_TEXT_PATH to any path).
CV_TEXT_PATH = os.environ.get("CV_TEXT_PATH", os.path.join(BASE_DIR, "cv.txt"))
CV_PROFILE_CACHE_PATH = os.path.join(DATA_DIR, "cv_profile.json")

# --- State ---
PROCESSED_IDS_PATH = os.path.join(DATA_DIR, "processed_ids.json")
LAST_RUN_PATH = os.path.join(DATA_DIR, "last_run.json")
SKIPPED_CANDIDATES_PATH = os.path.join(DATA_DIR, "skipped_candidates.csv")
# Full-detail companion to the CSV: one JSON object per line holding the scored text plus
# the requirement breakdown and reasoning, so a skip can be re-scored later even after the
# posting's link is dead, and the scoring method itself can be validated against it.
SKIPPED_DETAIL_PATH = os.path.join(DATA_DIR, "skipped_detail.jsonl")
GRAYED_JOB_IDS_PATH = os.path.join(DATA_DIR, "grayed_job_ids.json")
SEEN_CAREER_POSTINGS_PATH = os.path.join(DATA_DIR, "seen_career_postings.json")
# Retry-attempt tracking for stage 3 (verify) on a freshly-discovered opportunity
# that couldn't be verified yet -- distinct from a reply-derived row's in-sheet
# "(attempt N/M)" notes convention. A speculative discovery that never gets
# confirmed real after MAX_RESOLUTION_ATTEMPTS is dropped entirely (never written)
# rather than left as a dead "nr" placeholder row -- unlike a reply, which always
# represents a real event that happened even if we can't find the link.
PENDING_VERIFICATION_PATH = os.path.join(DATA_DIR, "pending_verification.json")

# Written by main.py when it stops early (3 consecutive Ollama failures) instead of
# fully draining the new-message queue; removed once a run completes cleanly.
# run_morning.bat checks for this before running scan_career_pages.py, so the
# proactive career-page scan never starts while the mail queue is still backed up --
# guards against the two competing for the same slow local LLM at once.
MAIL_QUEUE_INCOMPLETE_FLAG = os.path.join(DATA_DIR, "mail_queue_incomplete.flag")

# --- Career-page discovery ---
# Tried Google Programmable Search first, but "Search the entire web" was locked/grayed
# out on this account even after ruling out the usual causes (image search, safe search,
# a freshly created engine) -- so that path is unusable here. Left the code in place
# (job_page_fetcher.search_google_custom_full) in case it's ever unlocked later.
#
# SerpAPI (https://serpapi.com/) is used instead: free tier = 100 searches/month, a
# single API key, no site-restriction setup. Get a key by signing up, then:
#   [System.Environment]::SetEnvironmentVariable("SERPAPI_API_KEY", "your-key", "User")
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "")

GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY", "")
GOOGLE_SEARCH_CX = os.environ.get("GOOGLE_SEARCH_CX", "")

# --- Resolution retry ---
# A gapped row (blank url/description) gets retried by backfill_career_links.py on
# every scheduled run; after this many failed attempts it's marked "nr" instead of
# retried forever -- avoids indefinitely re-hitting a company career page that
# genuinely doesn't have the position, or a link that turned out unusable.
MAX_RESOLUTION_ATTEMPTS = 3

# A tracked position still sitting at "not applied yet" (or blank) this long after
# being saved is deprioritized automatically -- marked "nr", not "closed": this is
# a judgment call about priority (you didn't act on it), not confirmation the
# posting itself is actually gone, so it should still block a resurfaced duplicate
# the way any other manually-reviewed "nr" decision does. Checked in
# review_closed_positions.py, which runs before every mail scan.
STALE_NOT_APPLIED_DAYS = 21

# --- Thresholds ---
FIT_SCORE_THRESHOLD = 65
# Applied in code (never trusted to the LLM's own arithmetic) whenever the posting has
# a hard, non-substitutable required skill the candidate lacks -- e.g. "strong Java
# expertise required" with no listed alternative. Set below FIT_SCORE_THRESHOLD so such
# postings still fall out through the existing low-fit filter rather than needing a
# separate exclusion path.
HARD_REQUIREMENT_SCORE_CAP = 25
# How far back to scan on each run (dedup against processed_ids.json prevents reprocessing,
# so this only bounds query size/time). Was 30 (development/backfill testing value,
# left in by mistake); now running on a steady cadence, so 3 days gives a safety
# margin over the missed-run case without re-scanning a month of mail every time.
MAIL_LOOKBACK_DAYS = 3

# The career-site fuzzy title-match (job_page_fetcher.extract_snippet_near) has
# produced at least one confirmed false positive -- a "found" position that did not
# actually exist on the company's career page -- so this whole search path is
# disabled until the matching logic is tightened. While off, position_resolver
# still tries direct URLs found in the email body; it just never falls through to
# a company-directory career-page guess.
ENABLE_CAREER_SITE_SEARCH = False

# Gmail label applied by a separate Apps Script (running natively in Gmail every 15
# min on a broad job-related keyword match) -- the local agent only scans this label
# instead of the whole inbox, which keeps each run's volume small. Whether the mail
# also stays in Inbox or gets archived out doesn't matter to the agent -- it queries
# by label regardless of Inbox membership.
WORK_LABEL_NAME = "Work"

# Promo/commercial filtering is handled entirely by the Gmail Apps Script
# (moveCommercialsToSpam) -- this agent never sees that mail since it only scans
# the "Work" label.

# --- Commute / location filter ---
# Example values: commutable without a car from Haifa, per this project's original
# author -- the North district (Haifa area and its train-line towns) plus Tel
# Aviv/Herzliya (reachable by train). Replace both sets with your own commute range;
# classifier.is_location_excluded only rejects a location that clearly matches a
# known out-of-range city, so an empty EXCLUDED set effectively disables filtering.
ALLOWED_LOCATION_KEYWORDS = {
    "haifa", "north district", "krayot", "kiryat ata", "kiryat bialik", "kiryat motzkin",
    "kiryat yam", "kiryat tivon", "nesher", "yokneam", "migdal haemek", "afula", "nazareth",
    "acre", "akko", "nahariya", "karmiel", "tiberias", "zichron yaakov", "binyamina",
    "pardes hana", "atlit", "tirat carmel", "tel aviv", "telaviv", "herzliya", "herzelia",
    "remote", "hybrid",
}
EXCLUDED_LOCATION_KEYWORDS = {
    "raanana", "ra'anana", "hod hasharon", "jerusalem", "petah tikva", "petach tikva",
    "rishon lezion", "rishon le-zion", "rehovot", "ashdod", "ashkelon", "beer sheva",
    "beersheba", "netanya", "kfar saba", "ramat gan", "givatayim", "bnei brak", "modiin",
    "holon", "bat yam", "rosh haayin", "lod", "ramla", "nes ziona", "yavne",
}

# --- Local LLM (Ollama) --- same engine as jobAce/cv_matcher.py, fully free/offline.
# Single model everywhere. Was 14b (better at company-name extraction, e.g. catching
# less obvious names like "ZOLL Medical Corporation"), but that doesn't comfortably
# fit this machine's GPU -- degraded output from a model squeezed past its VRAM is
# worse than 7b's occasional extraction miss (see guardrails.py for the plausibility
# check that catches degraded-output hallucinations either way). Pick whichever model
# actually fits your hardware; 14b stays resident via keep_alive if you do use it.
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:7b"
OLLAMA_MODEL_CV_PARSE = "qwen2.5:7b"
OLLAMA_KEEP_ALIVE = "30m"
# None = don't override Ollama's default context (see llm_client.call_json for why:
# a num_ctx that differs from a shared runner's starves behind other projects'
# requests). Only set this if this is the sole project using this Ollama server.
OLLAMA_NUM_CTX = None
# Real incident (2026-09-22): another long-running task on this machine held the GPU
# for hours, so this agent's calls sat queued behind it in Ollama's single-GPU serial
# queue -- not stuck, just waiting their turn. With no runaway-generation risk left
# (see llm_client.py's num_predict cap), the only thing a call can still legitimately
# wait on is queue position, so the timeout is sized to tolerate that rather than
# abort a call that would have succeeded a few minutes later. 300s repeatedly hit "3
# consecutive failures" and aborted the whole run early during that window.
OLLAMA_TIMEOUT_SECONDS = 1200
OLLAMA_CV_PARSE_TIMEOUT_SECONDS = 1500
