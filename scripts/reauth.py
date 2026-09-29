"""Re-authorize both Gmail and Sheets in one run.

Google's OAuth "Testing" publish status (the only realistic option here -- Gmail's
modify scope is a RESTRICTED scope, and publishing to production with one requires a
paid CASA security assessment meant for companies, not a personal single-user tool)
expires refresh tokens after 7 days regardless of test-user status. When that happens,
gmail_client/sheets_client raise RefreshError and fire a toast notification -- this
script is the fix: it deletes both stale tokens and runs the interactive sign-in flow
for each, so you approve access twice in one sitting instead of hitting the prompt
piecemeal across separate scripts on different days.

Usage:
    python reauth.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config, gmail_client, sheets_client


def _reauth(label: str, token_path: str, get_service) -> None:
    if os.path.exists(token_path):
        os.remove(token_path)
        print(f"Removed stale {label} token.")
    print(f"Opening browser to reauthorize {label} -- approve access when prompted...")
    get_service()  # writes a fresh token file as a side effect
    print(f"{label} reauthorized.\n")


if __name__ == "__main__":
    _reauth("Gmail", config.TOKEN_GMAIL_PATH, gmail_client.get_gmail_service)
    _reauth("Sheets", config.TOKEN_SHEETS_PATH, sheets_client.get_sheets_service)
    print("Both reauthorized. Tokens are valid until Google's ~7-day Testing-mode expiry.")
