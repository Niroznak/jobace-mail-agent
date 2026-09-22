"""Gmail API auth, fetch unread messages, and mark-as-read."""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

import config
import notifier

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    snippet: str
    date_utc: str  # "YYYY-MM-DD", the email's actual received date -- never the run date


def get_gmail_service() -> Resource:
    creds = None
    if config.TOKEN_GMAIL_PATH and _path_exists(config.TOKEN_GMAIL_PATH):
        creds = Credentials.from_authorized_user_file(config.TOKEN_GMAIL_PATH, config.GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Silent failure here means the whole pipeline stops running with no
                # visible sign until someone happens to notice stale data days later
                # (this exact thing happened once already -- Google's "Testing"
                # publish-status OAuth apps expire refresh tokens after 7 days).
                notifier.notify_needs_review(
                    "Gmail authorization expired -- run any script once interactively "
                    "to reauthorize (a browser window will open)."
                )
                raise
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.OAUTH_CLIENT_SECRET_PATH, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.TOKEN_GMAIL_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _path_exists(path: str) -> bool:
    import os
    return os.path.exists(path)


_MAX_PAGES_SAFETY_CAP = 20  # 20 x 100 = 2000 messages -- a runaway-query circuit breaker, not a real limit


def list_recent_ids(service: Resource, max_results: int = 100, newer_than_days: int = 30) -> list[str]:
    """Lists every message ID tagged with the 'Work' Gmail label within the lookback
    window -- lightweight (no message bodies), so it's cheap to call even when the
    label holds hundreds of messages, and paginates rather than trusting one page to
    be enough.

    Real bug found 2026-09-19: fetch_recent used to hard-cap at a single page of 50
    results with no pagination -- every "Fetched N from Work label" log line all
    session long said exactly 50, meaning the label always has 50+ messages in the
    30-day window, so that cap was continuously binding. Any run where more than 50
    Work-labeled emails arrived since the previous run silently lost everything past
    the 50 most recent -- not fetched, not logged, not erred, just absent (confirmed
    case: an Exodigo application-confirmation email that never reached any run for
    two days). Callers should filter these IDs against already-processed ones
    (state.py) BEFORE fetching full content -- fetching every matching message's full
    body every run (now potentially hundreds, not 50) is wasteful and can trip
    Gmail's per-minute quota, as happened when this was first tried."""
    ids: list[str] = []
    page_token = None
    for _ in range(_MAX_PAGES_SAFETY_CAP):
        resp = service.users().messages().list(
            userId="me", q=f"label:{config.WORK_LABEL_NAME} newer_than:{newer_than_days}d",
            maxResults=max_results, pageToken=page_token,
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_message(service: Resource, msg_id: str) -> EmailMessage:
    raw = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    return _parse_message(raw)


def _decode_mime_words(text: str) -> str:
    try:
        parts = decode_header(text)
        return "".join(
            part.decode(encoding or "utf-8", errors="replace") if isinstance(part, bytes) else part
            for part, encoding in parts
        )
    except Exception:
        return text


def _parse_message(raw: dict) -> EmailMessage:
    headers = {h["name"].lower(): h["value"] for h in raw["payload"].get("headers", [])}
    sender_name, sender_email = parseaddr(headers.get("from", ""))
    sender_name = _decode_mime_words(sender_name)
    subject = _decode_mime_words(headers.get("subject", ""))
    body = _extract_body(raw["payload"])
    return EmailMessage(
        id=raw["id"],
        thread_id=raw["threadId"],
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body=body,
        snippet=raw.get("snippet", ""),
        date_utc=_parse_internal_date(raw.get("internalDate")),
    )


def _parse_internal_date(internal_date_ms: str | None) -> str:
    """Gmail's internalDate is the actual received time (epoch ms, UTC) -- used for
    date_saved instead of the run time, since a mail scan can lag behind when an
    email actually arrived (e.g. a reply from yesterday processed today)."""
    if not internal_date_ms:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _extract_body(payload: dict) -> str:
    """Prefers a real text/plain part. Some senders (e.g. Amazon's ATS) send HTML-only
    mail with no text/plain part at all -- for those, fall back to the HTML part and
    strip tags into readable text, rather than handing raw markup soup to the LLM
    (which produced no extractable role title/status detail at all in practice)."""
    plain = _find_part_by_mimetype(payload, "text/plain")
    if plain:
        return plain
    html = _find_part_by_mimetype(payload, "text/html")
    if html:
        return _html_to_text(html)
    if payload.get("body", {}).get("data"):
        return _html_to_text(_b64decode(payload["body"]["data"]))
    return ""


def _find_part_by_mimetype(payload: dict, mime_type: str) -> str:
    if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
        return _b64decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == mime_type and part.get("body", {}).get("data"):
            return _b64decode(part["body"]["data"])
    for part in payload.get("parts", []) or []:
        found = _find_part_by_mimetype(part, mime_type)
        if found:
            return found
    return ""


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _TAG_RE.sub("\n", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _b64decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def mark_as_read(service: Resource, message_id: str) -> None:
    service.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()
