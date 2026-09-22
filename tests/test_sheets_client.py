"""Tests for the pure/deterministic helpers in sheets_client.py -- no network calls."""
import sheets_client as sc


class TestNormalizeTitle:
    def test_strips_noise_words(self):
        assert sc.normalize_title("Senior Backend Developer") == "backend developer"

    def test_strips_dashes_and_case(self):
        assert sc.normalize_title("AI Engineer - Remote") == "ai engineer"

    def test_the_workmode_suffix_bug_case(self):
        # Real bug: LinkedIn baked "- remote/Tel-Aviv" into the title itself. Title
        # cleanup happens upstream (classifier.strip_workmode_suffix); this just
        # confirms normalize_title alone doesn't fully solve it (extra tokens survive).
        with_suffix = sc.normalize_title("AI Engineer - Technical Enablement - remote/Tel-Aviv")
        without_suffix = sc.normalize_title("AI Engineer - Technical Enablement")
        assert with_suffix != without_suffix


class TestNormalizeCompany:
    def test_strips_corporate_suffix(self):
        # Real bug: "Micron" (ATS confirmation) vs "Micron Technology" (original
        # listing) never matched under exact-string comparison.
        assert sc.normalize_company("Micron") == sc.normalize_company("Micron Technology")

    def test_does_not_falsely_merge_different_companies(self):
        assert sc.normalize_company("Meta") != sc.normalize_company("Metadata Inc")

    def test_blank_normalizes_to_empty(self):
        assert sc.normalize_company("") == ""
        assert sc.normalize_company("   ") == ""


class TestFindRowByCompanyAndTitle:
    ROWS = [
        {"company": "Mobileye", "title": "Experienced Deep Learning Optimization Engineer- Compilers Team", "_row": 46},
        {"company": "Mobileye", "title": "Experienced C++ Developer - Farm Team", "_row": 57},
        {"company": "Mobileye", "title": "Senior Python Infrastructure Developer", "_row": 66},
    ]

    def test_disambiguates_by_title_among_multiple_company_rows(self):
        # Real bug: an ATS confirmation naming the exact role wrongly landed on the
        # first Mobileye row (46) instead of the actual applied-to row (66).
        match = sc.find_row_by_company_and_title(self.ROWS, "Mobileye", "Senior Python Infrastructure Developer")
        assert match["_row"] == 66

    def test_blank_title_returns_none_not_a_guess(self):
        assert sc.find_row_by_company_and_title(self.ROWS, "Mobileye", "") is None

    def test_no_matching_title_returns_none(self):
        assert sc.find_row_by_company_and_title(self.ROWS, "Mobileye", "Totally Different Role") is None


class TestFindRowByCompanyAndDescription:
    def test_exact_description_match_same_company(self):
        desc = "We are seeking an Applied Scientist to build memory systems." * 3
        rows = [{"company": "Amazon", "description": desc, "_row": 67}]
        match = sc.find_row_by_company_and_description(rows, "Amazon", desc)
        assert match["_row"] == 67

    def test_blank_description_never_matches(self):
        rows = [{"company": "Amazon", "description": "", "_row": 67}]
        assert sc.find_row_by_company_and_description(rows, "Amazon", "") is None

    def test_different_company_never_matches_even_with_identical_text(self):
        desc = "Identical boilerplate text."
        rows = [{"company": "Amazon", "description": desc, "_row": 67}]
        assert sc.find_row_by_company_and_description(rows, "Google", desc) is None


class TestRowStatusPredicates:
    def test_is_row_closed(self):
        assert sc.is_row_closed({"status": "closed"})
        assert sc.is_row_closed({"status": "Closed"})
        assert not sc.is_row_closed({"status": "nr"})
        assert not sc.is_row_closed({"status": ""})

    def test_is_row_not_relevant(self):
        assert sc.is_row_not_relevant({"status": "nr"})
        assert not sc.is_row_not_relevant({"status": "closed"})

    def test_active_rows_excludes_only_closed(self):
        rows = [{"status": "closed"}, {"status": "nr"}, {"status": "applied"}, {"status": ""}]
        active = sc.active_rows(rows)
        assert len(active) == 3
        assert all(r["status"] != "closed" for r in active)

    def test_is_eligible_for_closure_only_pre_application_statuses(self):
        # Real bug: the automated review script could have overwritten a
        # reject/ATS_reject/applied status with "closed" if not guarded.
        assert sc.is_eligible_for_closure({"status": ""})
        assert sc.is_eligible_for_closure({"status": "not applied yet"})
        for terminal in ("applied", "interview", "offer", "reject", "ATS_reject", "closed", "nr"):
            assert not sc.is_eligible_for_closure({"status": terminal}), terminal


class TestAttemptCounter:
    def test_full_cycle_to_give_up(self):
        note = sc.format_attempt_note("Could not verify.", 1, 3)
        assert sc.parse_attempt_count(note) == 1

        attempt2 = sc.parse_attempt_count(note) + 1
        note2 = sc.format_attempt_note("Could not verify.", attempt2, 3)
        assert sc.parse_attempt_count(note2) == 2

        attempt3 = sc.parse_attempt_count(note2) + 1
        assert attempt3 >= 3  # should trigger give-up

    def test_no_marker_means_zero_attempts(self):
        assert sc.parse_attempt_count("just a plain note") == 0
        assert sc.parse_attempt_count("") == 0


class TestStatusHistory:
    def test_seeds_history_on_blank_notes(self):
        result = sc.append_status_history("", "not applied yet", "2026-09-11")
        assert result == "History: not applied yet-2026-09-11"

    def test_appends_to_existing_history(self):
        seeded = sc.append_status_history("", "not applied yet", "2026-09-11")
        updated = sc.append_status_history(seeded, "applied", "2026-09-13")
        assert updated == "History: not applied yet-2026-09-11, applied-2026-09-13"

    def test_preserves_non_history_notes(self):
        result = sc.append_status_history("candidate is a strong fit", "not applied yet", "2026-09-14")
        assert result == "candidate is a strong fit | History: not applied yet-2026-09-14"

    def test_multiple_interview_rounds_each_get_their_own_date(self):
        notes = sc.append_status_history("", "not applied yet", "2026-09-11")
        notes = sc.append_status_history(notes, "applied", "2026-09-13")
        notes = sc.append_status_history(notes, "interview", "2026-09-15")
        notes = sc.append_status_history(notes, "interview", "2026-09-22")
        assert notes == (
            "History: not applied yet-2026-09-11, applied-2026-09-13, "
            "interview-2026-09-15, interview-2026-09-22"
        )
