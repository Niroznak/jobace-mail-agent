"""The one entry point for pushing a new job-opportunity position into the jobAce
tracker sheet. Every ingestion path (main.py's four, plus scan_career_pages.py)
constructs a PositionRecord and calls append_position instead of hand-building a
fields dict -- a dataclass can't silently omit a required field the way a dict
literal can, and identity is checked here before the write happens (see
guardrails.py) rather than being caught after the fact.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from googleapiclient.discovery import Resource

from . import guardrails
from . import notifier
from . import sheets_client

logger = logging.getLogger(__name__)


@dataclass
class PositionRecord:
    """The one shape a new job-opportunity row can be built from. `company`, `title`,
    `status`, and `date_saved` have no default -- a call site that forgets one gets a
    TypeError at construction time, immediately, at the exact line that forgot it.
    Before this existed, each ingestion path hand-built its own plain dict; one of
    them (the "application reply, no existing row" branch) defaulted title to ""
    instead of falling back to the email subject like every other path, and nothing
    caught it until a blank row was already in the sheet."""
    company: str
    title: str
    status: str
    date_saved: str
    url: str = ""
    location: str = ""
    description: str = ""
    requirements: str = ""
    notes: str = ""
    job_id: str = ""
    contact_name: str = ""
    fit_score: str = ""

    def to_sheet_fields(self) -> dict:
        return dataclasses.asdict(self)


def append_position(service: Resource, record: PositionRecord) -> int:
    """Validates identity (company/title) before writing -- tags and notifies rather
    than dropping the row when incomplete, since a reply-derived row can be genuinely
    missing a title with no way to recover one. Returns the new row's 1-indexed
    sheet row number, or -1 if the write was refused outright (see below) -- the
    same sentinel already used for a dry-run's "not really written" row number, so
    every caller's existing handling of that value already covers this case too.

    A generic-listing title (see guardrails.looks_like_generic_listing_title) is
    refused entirely rather than flagged like a missing field would be: unlike a
    blank title, which might still represent a real opportunity we just couldn't
    name, "Open Positions" carries zero information -- there is nothing for a human
    to review, only a row to delete. Every known ingestion path already skips this
    earlier (before ever reaching here, to avoid wasting a fetch/score call on it);
    this is the last-resort backstop for any path that doesn't."""
    fields = record.to_sheet_fields()
    if guardrails.looks_like_generic_listing_title(fields.get("title", "")):
        logger.warning(
            "[REFUSED] generic listing title, not a specific position -- company=%r title=%r "
            "(this should have been skipped earlier; refusing to write it at all).",
            fields.get("company", ""), fields.get("title", ""),
        )
        return -1

    missing = guardrails.missing_identity_fields(fields)
    if missing:
        fields = guardrails.tag_incomplete(fields, missing)
        logger.warning(
            "[INCOMPLETE ROW] appending with missing %s -- company=%r title=%r job_id=%r",
            "/".join(missing), fields.get("company", ""), fields.get("title", ""), fields.get("job_id", ""),
        )
        notifier.notify_needs_review(
            f"New sheet row missing {'/'.join(missing)} -- flagged in notes, please fill in manually."
        )
    return sheets_client.append_row(service, fields)
