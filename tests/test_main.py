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


# TestResolveReplyTargetRow moved to test_guardrails.py -- the logic itself now
# lives in guardrails.resolve_reply_target_row (see guardrails.py's module
# docstring: every write-path decision belongs in one place, tested there).
