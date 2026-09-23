"""Dedup-key and status-transition logic shared by the verify and reconcile
pipeline stages. Lives under src/ (not scripts/) so both stage modules can import
it -- scripts/ depends on src/, never the other way around. scripts/main.py
re-exports both names so existing tests/usages of `main.job_id_for` /
`main.next_status` keep working unchanged.
"""
from __future__ import annotations

import hashlib

from .. import sheets_client


def next_status(old_status: str, signal: str) -> str:
    """Compute the granular status from the raw LLM signal (applied/interview/offer/
    rejected) plus the row's current status -- distinguishes an early/automated
    rejection (never got an interview) from a post-interview rejection, and never
    lets a stale "applied" ack downgrade a status that's already progressed further."""
    old = (old_status or "").strip().lower()
    if signal == "rejected":
        return "reject" if old == "interview" else "ATS_reject"
    if signal == "interview":
        return "interview"
    if signal == "offer":
        return "offer"
    if signal == "applied":
        return old_status if old in ("interview", "offer", "reject", "atsreject", "ats_reject") else "applied"
    return old_status


def job_id_for(company: str, title: str, position_id: str = "", requisition_id: str = "") -> str:
    """Dedup key priority:
    1. Employer's own requisition ID (e.g. "JR2023080"), when the real fetched job
       description contains one -- the MOST stable identifier, since a role that
       closes and reopens gets a brand new LinkedIn listing ID but keeps this one.
    2. LinkedIn's own numeric listing ID -- stable for as long as that specific
       listing stays open.
    3. Company + noise-stripped normalized title (exact match, not fuzzy) when
       neither ID is available, to avoid false-positive dedup between genuinely
       different roles at the same company.
    Company is deliberately NOT part of the key for 1/2, since the same real posting
    can get extracted with a slightly different company-name spelling."""
    if requisition_id.strip():
        key = f"req:{requisition_id.strip().lower()}"
    elif position_id.strip():
        key = f"pid:{position_id.strip().lower()}"
    else:
        key = f"{company.strip().lower()}|{sheets_client.normalize_title(title)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
