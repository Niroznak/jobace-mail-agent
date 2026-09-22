"""Tests for the pure dedup-key and status-transition logic in main.py."""
import main


class TestJobIdFor:
    def test_same_inputs_produce_same_key(self):
        a = main.job_id_for("Acme", "AI Engineer", "12345")
        b = main.job_id_for("Acme", "AI Engineer", "12345")
        assert a == b

    def test_different_position_id_produces_different_key(self):
        a = main.job_id_for("Acme", "AI Engineer", "12345")
        b = main.job_id_for("Acme", "AI Engineer", "67890")
        assert a != b

    def test_requisition_id_takes_priority_over_position_id(self):
        # Same requisition ID but different LinkedIn listing ID -> same key (catches
        # a role that closed and reopened under a new listing).
        a = main.job_id_for("Acme", "AI Engineer", "11111", "JR2023080")
        b = main.job_id_for("Acme", "AI Engineer", "99999", "JR2023080")
        assert a == b

    def test_company_not_part_of_key_when_position_id_present(self):
        # Deliberate: the same real posting can get extracted with a slightly
        # different company-name spelling across two emails.
        a = main.job_id_for("Acme Inc", "AI Engineer", "12345")
        b = main.job_id_for("Acme Corporation", "AI Engineer", "12345")
        assert a == b

    def test_falls_back_to_company_and_title_when_no_ids(self):
        a = main.job_id_for("Acme", "AI Engineer")
        b = main.job_id_for("Acme", "AI Engineer")
        assert a == b
        c = main.job_id_for("Acme", "Backend Engineer")
        assert a != c


class TestNextStatus:
    def test_applied_signal_from_not_applied_yet(self):
        assert main.next_status("not applied yet", "applied") == "applied"

    def test_applied_signal_never_downgrades_progressed_status(self):
        assert main.next_status("interview", "applied") == "interview"
        assert main.next_status("offer", "applied") == "offer"

    def test_interview_signal_always_wins(self):
        assert main.next_status("applied", "interview") == "interview"
        assert main.next_status("not applied yet", "interview") == "interview"

    def test_rejected_after_interview_is_reject(self):
        assert main.next_status("interview", "rejected") == "reject"

    def test_rejected_without_interview_is_ats_reject(self):
        assert main.next_status("applied", "rejected") == "ATS_reject"
        assert main.next_status("not applied yet", "rejected") == "ATS_reject"

    def test_offer_signal(self):
        assert main.next_status("interview", "offer") == "offer"


class TestResolveReplyTargetRow:
    def test_single_row_for_company_is_unambiguous_when_reply_has_no_title(self):
        # The common case: one tracked role per company, and the reply email itself
        # carries no extractable title (e.g. a terse ack) -- nothing else it could be
        # about, since there's no title information to conflict with.
        rows = [{"company": "ONE datAI", "title": "Data & AI Leader", "_row": 59}]
        row, ambiguous, tier = main._resolve_reply_target_row(rows, "ONE datAI", "")
        assert row["_row"] == 59
        assert ambiguous == []
        assert tier == "single_company_row"

    def test_single_row_but_conflicting_title_is_flagged_not_guessed(self):
        # Real bug: two same-day Mercor rejections ("Excel Expert - Finance" and
        # "Excel Expert - General") silently merged into one row because the single-
        # company-row fallback trusted any title, even one that actively conflicts
        # with the one tracked row's own specific title. A reply that states a real,
        # different title is evidence of a second, distinct position -- not "the
        # LLM just phrased it differently" -- and must be flagged, not guessed.
        rows = [{"company": "Mercor", "title": "Excel Expert - Finance", "_row": 94}]
        row, ambiguous, tier = main._resolve_reply_target_row(rows, "Mercor", "Excel Expert - General")
        assert row is None
        assert ambiguous == [rows[0]]
        assert tier == "title_conflict"

    def test_multiple_rows_disambiguated_by_exact_title(self):
        rows = [
            {"company": "Mobileye", "title": "Role A", "_row": 46},
            {"company": "Mobileye", "title": "Role B", "_row": 66},
        ]
        row, ambiguous, tier = main._resolve_reply_target_row(rows, "Mobileye", "Role B")
        assert row["_row"] == 66
        assert ambiguous == []
        assert tier == "title_match"

    def test_multiple_rows_no_title_match_is_ambiguous_not_guessed(self):
        # Real bug: this case used to silently pick the first row (wrong result for
        # Mobileye). Now it must return no match plus the candidate list.
        rows = [
            {"company": "Mobileye", "title": "Role A", "_row": 46},
            {"company": "Mobileye", "title": "Role B", "_row": 57},
            {"company": "Mobileye", "title": "Role C", "_row": 66},
        ]
        row, ambiguous, tier = main._resolve_reply_target_row(rows, "Mobileye", "Some Unrelated Wording")
        assert row is None
        assert len(ambiguous) == 3

    def test_no_rows_for_company_returns_none_no_ambiguity(self):
        row, ambiguous, tier = main._resolve_reply_target_row([], "Unknown Co", "Some Role")
        assert row is None
        assert ambiguous == []
