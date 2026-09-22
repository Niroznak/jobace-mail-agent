"""Tests for validate_sheet.py's row-filtering logic -- no network/Sheets calls.

Real incident: the identity-completeness check ran against every row regardless of
status, so a row already marked nr/closed with a historical gap (e.g. blank title)
got re-flagged and re-notified on every single run forever, even though nothing in
this pipeline ever revisits a terminal row. _is_terminal/_missing_content must skip
closed/nr rows the same way review_closed_positions.py already grays them out.
"""
import validate_sheet as vs


def _row(**overrides):
    base = {"_row": 1, "company": "Acme", "title": "Engineer", "status": "not applied yet", "url": "", "description": ""}
    base.update(overrides)
    return base


class TestIsTerminal:
    def test_closed_is_terminal(self):
        assert vs._is_terminal(_row(status="closed"))

    def test_nr_is_terminal(self):
        assert vs._is_terminal(_row(status="nr"))

    def test_active_status_is_not_terminal(self):
        assert not vs._is_terminal(_row(status="applied"))

    def test_rejected_is_not_terminal(self):
        # Only closed/nr are graced out by review_closed_positions.py -- a rejection
        # is still a real content-completeness candidate (backfill_career_links.py
        # still treats it as a valid target), just not a fresh opportunity anymore.
        assert not vs._is_terminal(_row(status="ATS_reject"))


class TestMissingContentSkipsTerminalRows:
    def test_terminal_row_with_no_url_or_description_is_not_flagged(self):
        # Real case: a "nr" row with a blank title never gets backfilled -- flagging
        # its missing content every run is pure noise, not an actionable gap.
        assert not vs._missing_content(_row(status="nr", url="", description=""))

    def test_active_row_with_no_url_or_description_is_flagged(self):
        assert vs._missing_content(_row(status="applied", url="", description=""))

    def test_active_row_with_a_url_is_not_flagged(self):
        assert not vs._missing_content(_row(status="applied", url="https://example.com", description=""))


class TestRunSkipsTerminalRowsEntirely:
    def test_terminal_row_missing_title_never_reaches_identity_problems(self, monkeypatch):
        # The real row-3 case: company="Chalk", title="", status="nr" -- must not
        # appear in identity_problems at all, not even once, since nothing will ever
        # act on it.
        rows = [
            {"_row": 3, "company": "Chalk", "title": "", "status": "nr", "url": "", "description": "", "notes": ""},
            {"_row": 6, "company": "SCD", "title": "Algorithm Engineer", "status": "interview", "url": "https://x", "description": "real text", "notes": ""},
        ]
        monkeypatch.setattr(vs.sheets_client, "get_sheets_service", lambda: object())
        monkeypatch.setattr(vs.sheets_client, "fetch_all_rows", lambda service: rows)
        monkeypatch.setattr(vs, "config", type("C", (), {"SHEET_ID": "fake-id"}))
        notified = []
        monkeypatch.setattr(vs.notifier, "notify_needs_review", lambda msg: notified.append(msg))

        result = vs.run()

        assert result["identity_problems"] == []
        assert result["content_problems"] == []
        assert notified == []
