"""Tests for guardrails.py -- the single place every write path's "is this safe to
insert, and is it the right row?" checks live. No network/Sheets/LLM calls.
"""
from mail_agent import guardrails


class TestMissingIdentityFields:
    def test_both_present_is_complete(self):
        assert guardrails.missing_identity_fields({"company": "Acme", "title": "Engineer"}) == []

    def test_blank_title_is_missing(self):
        assert guardrails.missing_identity_fields({"company": "Acme", "title": ""}) == ["title"]

    def test_whitespace_only_counts_as_missing(self):
        assert guardrails.missing_identity_fields({"company": "  ", "title": "Engineer"}) == ["company"]

    def test_both_missing(self):
        assert guardrails.missing_identity_fields({}) == ["company", "title"]


class TestTagIncomplete:
    def test_prepends_review_tag_to_existing_notes(self):
        fields = {"notes": "Some real note."}
        tagged = guardrails.tag_incomplete(fields, ["title"])
        assert tagged["notes"] == "[NEEDS REVIEW: missing title] Some real note."

    def test_works_with_blank_notes(self):
        tagged = guardrails.tag_incomplete({}, ["company", "title"])
        assert tagged["notes"] == "[NEEDS REVIEW: missing company/title]"

    def test_never_mutates_the_original_dict(self):
        # A shared row-in-progress dict must not be mutated out from under a caller
        # still holding a reference to it.
        original = {"notes": "x"}
        guardrails.tag_incomplete(original, ["title"])
        assert original == {"notes": "x"}


class TestLooksLikeHallucinatedTriage:
    def test_normal_short_note_and_valid_status_is_trusted(self):
        assert not guardrails.looks_like_hallucinated_triage("Acknowledged application.", "applied")

    def test_blank_status_is_valid_for_non_reply_categories(self):
        assert not guardrails.looks_like_hallucinated_triage("", "")

    def test_invalid_status_value_is_flagged(self):
        assert guardrails.looks_like_hallucinated_triage("short note", "maybe-rejected")

    def test_implausibly_long_notes_is_flagged(self):
        # Real incident: ~400 chars of leaked Hebrew/Chinese chain-of-thought
        # reasoning landed in "notes" alongside an invented "rejected" status for a
        # role with no reject email at all.
        leaked_reasoning = "x" * 300
        assert guardrails.looks_like_hallucinated_triage(leaked_reasoning, "rejected")

    def test_notes_right_at_the_boundary_is_not_flagged(self):
        assert not guardrails.looks_like_hallucinated_triage("x" * guardrails.MAX_TRIAGE_NOTES_CHARS, "applied")


class TestResolveReplyTargetRow:
    def test_single_row_for_company_is_unambiguous_when_reply_has_no_title(self):
        # The common case: one tracked role per company, and the reply email itself
        # carries no extractable title (e.g. a terse ack) -- nothing else it could be
        # about, since there's no title information to conflict with.
        rows = [{"company": "ONE datAI", "title": "Data & AI Leader", "_row": 59}]
        row, ambiguous, tier = guardrails.resolve_reply_target_row(rows, "ONE datAI", "")
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
        row, ambiguous, tier = guardrails.resolve_reply_target_row(rows, "Mercor", "Excel Expert - General")
        assert row is None
        assert ambiguous == [rows[0]]
        assert tier == "title_conflict"

    def test_multiple_rows_disambiguated_by_exact_title(self):
        rows = [
            {"company": "Mobileye", "title": "Role A", "_row": 46},
            {"company": "Mobileye", "title": "Role B", "_row": 66},
        ]
        row, ambiguous, tier = guardrails.resolve_reply_target_row(rows, "Mobileye", "Role B")
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
        row, ambiguous, tier = guardrails.resolve_reply_target_row(rows, "Mobileye", "Some Unrelated Wording")
        assert row is None
        assert len(ambiguous) == 3

    def test_no_rows_for_company_returns_none_no_ambiguity(self):
        row, ambiguous, tier = guardrails.resolve_reply_target_row([], "Unknown Co", "Some Role")
        assert row is None
        assert ambiguous == []
