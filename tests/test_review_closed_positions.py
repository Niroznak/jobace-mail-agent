"""Tests for review_closed_positions.py's pure staleness check -- no network/Sheets
calls. This runs before every mail scan (see run_mail_agent.bat), auto-deprioritizing
a tracked row that's sat at a pre-application status for config.STALE_NOT_APPLIED_DAYS+
days -- a priority judgment call (marked "nr", not "closed": the posting itself may
still be live, the candidate just never acted on it).
"""
from datetime import datetime

import review_closed_positions as rcp


def _row(date_saved: str, status: str = "not applied yet") -> dict:
    return {"_row": 1, "company": "Acme", "title": "Engineer", "status": status, "date_saved": date_saved}


TODAY = datetime(2026, 9, 23)


class TestIsStaleNotApplied:
    def test_recently_saved_row_is_not_stale(self):
        assert not rcp._is_stale_not_applied(_row("2026-09-20"), TODAY)

    def test_exactly_at_threshold_is_stale(self):
        old_date = "2026-09-02"  # exactly 21 days before 2026-09-23
        assert rcp._is_stale_not_applied(_row(old_date), TODAY)

    def test_well_past_threshold_is_stale(self):
        assert rcp._is_stale_not_applied(_row("2026-08-01"), TODAY)

    def test_already_applied_is_never_stale(self):
        # Only a pre-application status (blank/"not applied yet") is eligible --
        # an old "applied"/"interview"/"rejected" row has real history, not neglect.
        assert not rcp._is_stale_not_applied(_row("2026-08-01", status="applied"), TODAY)
        assert not rcp._is_stale_not_applied(_row("2026-08-01", status="interview"), TODAY)
        assert not rcp._is_stale_not_applied(_row("2026-08-01", status="offer"), TODAY)

    def test_already_closed_or_nr_is_never_re_flagged(self):
        assert not rcp._is_stale_not_applied(_row("2026-08-01", status="closed"), TODAY)
        assert not rcp._is_stale_not_applied(_row("2026-08-01", status="nr"), TODAY)

    def test_blank_date_saved_is_safe(self):
        assert not rcp._is_stale_not_applied(_row(""), TODAY)

    def test_malformed_date_saved_is_safe(self):
        assert not rcp._is_stale_not_applied(_row("not-a-date"), TODAY)
