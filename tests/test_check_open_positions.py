"""Tests for check_open_positions.py -- the daily integrity check merging what were
review_closed_positions.py (liveness/staleness) and validate_sheet.py (completeness),
plus a new duplicate check. No network/Sheets calls.
"""
from datetime import datetime

import check_open_positions as cop

TODAY = datetime(2026, 9, 23)


def _row(**overrides):
    base = {"_row": 1, "company": "Acme", "title": "Engineer", "status": "not applied yet",
            "date_saved": "2026-09-20", "url": "", "description": ""}
    base.update(overrides)
    return base


# --- ported from test_review_closed_positions.py ---

class TestIsStaleNotApplied:
    def test_recently_saved_row_is_not_stale(self):
        assert not cop._is_stale_not_applied(_row(date_saved="2026-09-20"), TODAY)

    def test_exactly_at_threshold_is_stale(self):
        assert cop._is_stale_not_applied(_row(date_saved="2026-09-02"), TODAY)  # exactly 21 days before

    def test_well_past_threshold_is_stale(self):
        assert cop._is_stale_not_applied(_row(date_saved="2026-08-01"), TODAY)

    def test_already_applied_is_never_stale(self):
        assert not cop._is_stale_not_applied(_row(date_saved="2026-08-01", status="applied"), TODAY)
        assert not cop._is_stale_not_applied(_row(date_saved="2026-08-01", status="interview"), TODAY)

    def test_already_closed_or_nr_is_never_re_flagged(self):
        assert not cop._is_stale_not_applied(_row(date_saved="2026-08-01", status="closed"), TODAY)
        assert not cop._is_stale_not_applied(_row(date_saved="2026-08-01", status="nr"), TODAY)

    def test_blank_or_malformed_date_saved_is_safe(self):
        assert not cop._is_stale_not_applied(_row(date_saved=""), TODAY)
        assert not cop._is_stale_not_applied(_row(date_saved="not-a-date"), TODAY)


class TestGrayedKey:
    def test_uses_job_id_when_present(self):
        assert cop._grayed_key({"_row": 42, "job_id": "abc123"}) == "abc123"

    def test_falls_back_to_row_number_when_job_id_blank_or_missing(self):
        assert cop._grayed_key({"_row": 8, "job_id": ""}) == "row:8"
        assert cop._grayed_key({"_row": 8}) == "row:8"

    def test_two_rows_with_blank_job_id_get_distinct_keys(self):
        assert cop._grayed_key({"_row": 8, "job_id": ""}) != cop._grayed_key({"_row": 9, "job_id": ""})


# --- ported from test_validate_sheet.py ---

class TestIsTerminal:
    def test_closed_and_nr_are_terminal(self):
        assert cop._is_terminal(_row(status="closed"))
        assert cop._is_terminal(_row(status="nr"))

    def test_other_statuses_are_not_terminal(self):
        assert not cop._is_terminal(_row(status="applied"))
        assert not cop._is_terminal(_row(status="ATS_reject"))


class TestMissingContent:
    def test_terminal_row_is_never_flagged(self):
        assert not cop._missing_content(_row(status="nr", url="", description=""))

    def test_active_row_with_neither_url_nor_description_is_flagged(self):
        assert cop._missing_content(_row(status="applied", url="", description=""))

    def test_active_row_with_a_url_is_not_flagged(self):
        assert not cop._missing_content(_row(status="applied", url="https://example.com"))


# --- new: duplicate detection ---

class TestFindDuplicateGroups:
    def test_flags_two_active_rows_with_same_company_and_title(self):
        rows = [_row(_row=10, company="Acme", title="Engineer"),
                _row(_row=11, company="Acme", title="Engineer")]
        groups = cop.find_duplicate_groups(rows)
        assert len(groups) == 1 and {r["_row"] for r in groups[0]} == {10, 11}

    def test_parenthetical_company_variant_is_still_flagged_as_duplicate(self):
        # Real incident: "CaliAlfa" vs "CaliAlfa (Previously Alfabet)" -- fixed in
        # dedup.job_id_for by normalizing the company name first.
        rows = [_row(_row=10, company="CaliAlfa", title="Senior Data Scientist"),
                _row(_row=11, company="CaliAlfa (Previously Alfabet)", title="Senior Data Scientist")]
        groups = cop.find_duplicate_groups(rows)
        assert len(groups) == 1

    def test_different_titles_are_not_duplicates(self):
        rows = [_row(_row=10, company="Acme", title="Engineer"),
                _row(_row=11, company="Acme", title="Manager")]
        assert cop.find_duplicate_groups(rows) == []

    def test_terminal_rows_are_excluded_from_duplicate_check(self):
        rows = [_row(_row=10, company="Acme", title="Engineer", status="nr"),
                _row(_row=11, company="Acme", title="Engineer", status="nr")]
        assert cop.find_duplicate_groups(rows) == []

    def test_rows_missing_company_or_title_are_skipped_not_grouped_together(self):
        rows = [_row(_row=10, company="", title="Engineer"),
                _row(_row=11, company="Acme", title="")]
        assert cop.find_duplicate_groups(rows) == []

    def test_three_way_duplicate_reports_as_one_group(self):
        rows = [_row(_row=n, company="Acme", title="Engineer") for n in (10, 11, 12)]
        groups = cop.find_duplicate_groups(rows)
        assert len(groups) == 1 and len(groups[0]) == 3
