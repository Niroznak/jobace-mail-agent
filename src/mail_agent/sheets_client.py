"""Google Sheets API auth and row read/upsert for the jobAce tracker."""
from __future__ import annotations

import logging
import os
import re

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from . import config
from . import notifier

logger = logging.getLogger(__name__)


def get_sheets_service() -> Resource:
    creds = None
    if os.path.exists(config.TOKEN_SHEETS_PATH):
        creds = Credentials.from_authorized_user_file(config.TOKEN_SHEETS_PATH, config.SHEETS_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                notifier.notify_needs_review(
                    "Sheets authorization expired -- run any script once interactively "
                    "to reauthorize (a browser window will open)."
                )
                raise
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.OAUTH_CLIENT_SECRET_PATH, config.SHEETS_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.TOKEN_SHEETS_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("sheets", "v4", credentials=creds)


def ensure_header(service: Resource) -> None:
    """Make sure columns Q/R/S (job_id, contact_name, fit_score) exist; never touches A-P."""
    result = service.spreadsheets().values().get(
        spreadsheetId=config.SHEET_ID, range=f"{config.SHEET_TAB}!A1:S1"
    ).execute()
    header = result.get("values", [[]])
    header_row = header[0] if header else []
    if len(header_row) < 19:
        service.spreadsheets().values().update(
            spreadsheetId=config.SHEET_ID,
            range=f"{config.SHEET_TAB}!Q1:S1",
            valueInputOption="RAW",
            body={"values": [["job_id", "contact_name", "fit_score"]]},
        ).execute()


def fetch_all_rows(service: Resource) -> list[dict]:
    result = service.spreadsheets().values().get(
        spreadsheetId=config.SHEET_ID, range=f"{config.SHEET_TAB}!{config.SHEET_RANGE_FULL}"
    ).execute()
    raw_rows = result.get("values", [])
    rows = []
    for i, row in enumerate(raw_rows):
        padded = row + [""] * (len(config.SHEET_COLUMNS) - len(row))
        job = dict(zip(config.SHEET_COLUMNS, padded))
        job["_row"] = i + 2
        rows.append(job)
    return rows


def find_row_by_job_id(rows: list[dict], job_id: str) -> dict | None:
    for row in rows:
        if row.get("job_id") == job_id:
            return row
    return None


def find_row_by_company_and_description(rows: list[dict], company: str, description: str) -> dict | None:
    """Catches the case where job_id_for's normal keys (requisition ID, then
    LinkedIn listing ID) both miss a real duplicate -- e.g. LinkedIn syndicating the
    exact same employer posting through two different channels (a job-alert digest
    and a "jobs qualification board" recommendation), each getting its own distinct
    listing ID despite being byte-for-byte the same job description. An exact
    (whitespace-trimmed) description match for the same company is essentially
    impossible to hit by coincidence between two genuinely different postings."""
    description = (description or "").strip()
    if not description:
        return None
    company_lower = company.strip().lower()
    for row in rows:
        if row.get("company", "").strip().lower() == company_lower and row.get("description", "").strip() == description:
            return row
    return None


# Conservative, exact-after-stripping normalization -- same low-false-positive spirit
# as _TITLE_NOISE_WORDS below. Only strips common corporate suffixes (e.g. "Micron"
# vs "Micron Technology"), never merges genuinely different company names.
_COMPANY_NOISE_WORDS = {
    "inc", "incorporated", "ltd", "llc", "corp", "corporation", "co", "company",
    "group", "technologies", "technology", "systems", "solutions", "labs", "holdings",
}


def normalize_company(name: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", (name or "").lower())
    return " ".join(w for w in words if w not in _COMPANY_NOISE_WORDS)


def find_row_by_company(rows: list[dict], company: str) -> dict | None:
    """First row matching this company (any role) -- fallback for application-reply
    status updates when no title is known. Ambiguous when a company has multiple
    tracked roles: prefer find_row_by_company_and_title first whenever a title is
    available."""
    normalized = normalize_company(company)
    if not normalized:
        return None
    for row in rows:
        if normalize_company(row.get("company", "")) == normalized:
            return row
    return None


def find_row_by_company_and_title(rows: list[dict], company: str, title: str) -> dict | None:
    """Disambiguates which of a company's multiple tracked rows a reply is about --
    e.g. an ATS confirmation naming the exact role applied to. Falls back to None
    (not a company-only match) when title is blank or matches no tracked row, so the
    caller can fall back to find_row_by_company deliberately rather than silently
    guessing the wrong role among several."""
    normalized_title = normalize_title(title)
    if not normalized_title:
        return None
    for row in find_rows_by_company(rows, company):
        if normalize_title(row.get("title", "")) == normalized_title:
            return row
    return None


_HISTORY_PREFIX = "History: "


def append_status_history(notes: str, status: str, date_str: str) -> str:
    """Appends a status-transition record to `notes` so changing a row's status never
    loses track of when it held a previous one -- e.g. seeding "History: not applied
    yet-2026-09-11" at creation, then "...applied-2026-09-13" once it changes. The
    history segment is always the trailing part of notes, separated from any other
    note content by " | ", and is only ever appended to, never rewritten."""
    notes = (notes or "").strip()
    entry = f"{status}-{date_str}"
    if _HISTORY_PREFIX in notes:
        base, _, history = notes.partition(_HISTORY_PREFIX)
        base = base.strip(" |")
        new_history = f"{history.strip()}, {entry}"
    else:
        base = notes
        new_history = entry
    history_segment = f"{_HISTORY_PREFIX}{new_history}"
    return f"{base} | {history_segment}" if base else history_segment


def is_row_closed(row: dict) -> bool:
    return row.get("status", "").strip().lower() == "closed"


def is_row_not_relevant(row: dict) -> bool:
    """"nr" is a manual verdict (the user reviewed the position and decided it's not
    worth pursuing) -- distinct from "closed" (the posting itself went away). Both
    render gray, but only "closed" is excluded from dedup: an "nr" row should keep
    blocking the same job_id if it turns up again in mail."""
    return row.get("status", "").strip().lower() == "nr"


_ATTEMPT_RE = re.compile(r"\(attempt (\d+)/(\d+)\)")


def format_attempt_note(base_note: str, attempt: int, max_attempts: int) -> str:
    return f"{base_note} (attempt {attempt}/{max_attempts})"


def parse_attempt_count(notes: str) -> int:
    match = _ATTEMPT_RE.search(notes or "")
    return int(match.group(1)) if match else 0


# Once a position has any real application-pipeline history (applied, an interview,
# an offer, or a rejection), that status must never be overwritten with "closed" --
# the posting disappearing later doesn't erase what actually happened with it.
_PRE_APPLICATION_STATUSES = {"", config.STATUS_NOT_APPLIED_YET.lower()}


def is_eligible_for_closure(row: dict) -> bool:
    return row.get("status", "").strip().lower() in _PRE_APPLICATION_STATUSES


def active_rows(rows: list[dict]) -> list[dict]:
    """Rows not marked closed -- new-position dedup should only ever be checked
    against these, so a role that closes and reopens as a fresh listing is treated as
    new rather than silently skipped as a duplicate of the dead row."""
    return [row for row in rows if not is_row_closed(row)]


def find_rows_by_company(rows: list[dict], company: str) -> list[dict]:
    normalized = normalize_company(company)
    if not normalized:
        return []
    return [row for row in rows if normalize_company(row.get("company", "")) == normalized]


# Deliberately NOT using fuzzy/ratio-based title similarity for dedup: tested against
# real postings, "Algorithm Developer" vs "Senior Backend Developer" scored 0.61 and
# "AI Engineer" vs "Data Engineer" scored 0.83 with difflib.SequenceMatcher -- both
# would false-positive as duplicates and silently hide a genuinely different role.
# Exact match (on a real position ID when available, else a noise-stripped normalized
# title) avoids that false-positive risk at the cost of missing some reworded dupes.
_TITLE_NOISE_WORDS = {
    "senior", "junior", "sr", "jr", "lead", "principal", "staff", "remote", "hybrid",
    "onsite", "full-time", "part-time", "position", "role", "job", "opening", "-", "–", "—",
}


def normalize_title(title: str) -> str:
    import re
    words = re.findall(r"[a-zA-Z0-9]+", title.lower())
    return " ".join(w for w in words if w not in _TITLE_NOISE_WORDS)


def update_row_fields(service: Resource, row_number: int, fields: dict) -> None:
    """Update only the given column names for a specific row (1-indexed sheet row)."""
    if row_number < 2:
        raise ValueError(f"Refusing to update invalid row_number={row_number}")
    col_letters = {name: _col_letter(i) for i, name in enumerate(config.SHEET_COLUMNS)}
    data = []
    for name, value in fields.items():
        if name not in col_letters:
            continue
        col = col_letters[name]
        data.append({
            "range": f"{config.SHEET_TAB}!{col}{row_number}",
            "values": [[value]],
        })
    if not data:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=config.SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def append_row(service: Resource, fields: dict) -> int:
    """Mechanical write only -- appends a row and returns its real 1-indexed sheet
    row number. No validation here; that lives in guardrails.py and is applied by
    position_sheet.append_position, the intended entry point for every ingestion
    path. Call this directly only for a full fields dict you've already validated
    yourself (e.g. a data-repair script)."""
    row = [fields.get(col, "") for col in config.SHEET_COLUMNS]
    result = service.spreadsheets().values().append(
        spreadsheetId=config.SHEET_ID,
        range=f"{config.SHEET_TAB}!A1:R1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    updated_range = result["updates"]["updatedRange"]  # e.g. "'Jobs'!A15:R15"
    row_number = int(re.search(r"![A-Z]+(\d+)", updated_range).group(1))
    return row_number


def delete_rows(service: Resource, row_numbers: list[int]) -> None:
    """Delete sheet rows (1-indexed) and shift the rest up."""
    if not row_numbers:
        return
    meta = service.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"] for s in meta["sheets"]
        if s["properties"]["title"] == config.SHEET_TAB
    )
    requests = [
        {"deleteDimension": {"range": {
            "sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": row - 1, "endIndex": row,
        }}}
        for row in sorted(row_numbers, reverse=True)  # delete bottom-up so indices stay valid
    ]
    service.spreadsheets().batchUpdate(spreadsheetId=config.SHEET_ID, body={"requests": requests}).execute()


def refresh_basic_filter(service: Resource) -> None:
    """Re-applies the sheet's existing basic filter (if any) so status/formatting
    changes made via the API are reflected in which rows are hidden -- Sheets doesn't
    always recompute a live filter's hidden rows from an API-side edit until the
    filter itself is touched. No-op if no basic filter is configured on the tab."""
    meta = service.spreadsheets().get(
        spreadsheetId=config.SHEET_ID, fields="sheets(properties,basicFilter)"
    ).execute()
    sheet = next(
        (s for s in meta["sheets"] if s["properties"]["title"] == config.SHEET_TAB), None
    )
    if not sheet or "basicFilter" not in sheet:
        return
    sheet_id = sheet["properties"]["sheetId"]
    basic_filter = sheet["basicFilter"]
    service.spreadsheets().batchUpdate(
        spreadsheetId=config.SHEET_ID,
        body={"requests": [{"clearBasicFilter": {"sheetId": sheet_id}}]},
    ).execute()
    service.spreadsheets().batchUpdate(
        spreadsheetId=config.SHEET_ID,
        body={"requests": [{"setBasicFilter": {"filter": basic_filter}}]},
    ).execute()


def set_row_text_color(service: Resource, row_number: int, rgb: tuple[float, float, float]) -> None:
    """Sets the text color for an entire row (columns A:S) -- used to gray out rows
    marked as closed/not-relevant while keeping the data (never deletes)."""
    if row_number < 2:
        raise ValueError(f"Refusing to format invalid row_number={row_number}")
    meta = service.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"] for s in meta["sheets"]
        if s["properties"]["title"] == config.SHEET_TAB
    )
    r, g, b = rgb
    request = {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_number - 1,
                "endRowIndex": row_number,
                "startColumnIndex": 0,
                "endColumnIndex": len(config.SHEET_COLUMNS),
            },
            "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": {"red": r, "green": g, "blue": b}}}},
            "fields": "userEnteredFormat.textFormat.foregroundColor",
        }
    }
    service.spreadsheets().batchUpdate(spreadsheetId=config.SHEET_ID, body={"requests": [request]}).execute()


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
