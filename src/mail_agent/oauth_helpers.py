"""Shared OAuth token loading/refresh/reauth logic used by gmail_client and
sheets_client -- kept in one place so the self-healing behavior below applies
identically to both instead of drifting between two near-duplicate copies.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from . import config
from . import notifier

logger = logging.getLogger(__name__)

# How long to wait for the user to click through the browser consent screen before
# giving up. Long enough for an attended single-click run (double-clicking a .bat and
# approving access takes seconds, not minutes); short enough that an unattended/headless
# run (e.g. a future Task Scheduler entry with nobody at the keyboard) doesn't hang
# indefinitely waiting for a click that will never come -- it times out and raises,
# same as the old behavior, instead of blocking the process forever.
_REAUTH_TIMEOUT_SECONDS = 180


def _run_interactive_flow(client_secrets_path: str, scopes: list[str]) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, scopes)
    return flow.run_local_server(port=0)


def get_credentials(label: str, token_path: str, scopes: list[str]) -> Credentials:
    """Loads, refreshes, or (re)authorizes credentials for one API, writing the result
    back to `token_path`. `label` is only used in log/notification text ("Gmail",
    "Sheets").

    Real incident: an expired refresh token (Google's Testing-mode ~7-day expiry) used
    to just notify and raise, silently stopping the whole pipeline until someone
    noticed. Now it auto-opens the browser reauth flow itself -- the same one-click
    single-command fix scripts/reauth.py performs by hand -- so a normal attended run
    self-heals instead of crashing. The timeout above is the safety net for the one
    case that shouldn't auto-launch a browser and wait forever: a truly unattended run."""
    creds = None
    if token_path and os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            logger.warning("%s refresh token expired/revoked -- attempting automatic reauth.", label)
            notifier.notify_needs_review(
                f"{label} authorization expired -- opening a browser to reauthorize automatically "
                f"(approve access when prompted)."
            )
            creds = _reauth_with_timeout(label, scopes)
    else:
        creds = _reauth_with_timeout(label, scopes)

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def _reauth_with_timeout(label: str, scopes: list[str]) -> Credentials:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_interactive_flow, config.OAUTH_CLIENT_SECRET_PATH, scopes)
        try:
            return future.result(timeout=_REAUTH_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            notifier.notify_needs_review(
                f"{label} reauthorization timed out waiting for browser approval -- "
                f"run reauth.py manually when you're at the computer."
            )
            raise RefreshError(f"{label} reauth timed out after {_REAUTH_TIMEOUT_SECONDS}s with no approval")
