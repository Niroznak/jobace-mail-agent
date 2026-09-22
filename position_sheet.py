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

import guardrails
import notifier
import sheets_client

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
    sheet row number."""
    fields = record.to_sheet_fields()
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
