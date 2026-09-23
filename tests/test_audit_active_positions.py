"""Tests for audit_active_positions.py's per-row check logic -- mocks
classifier.confirm_company_name and job_page_fetcher (the only external calls);
no real network/Ollama calls.
"""
import audit_active_positions as audit
from mail_agent import classifier
from mail_agent import job_page_fetcher


def _row(**overrides) -> dict:
    base = {
        "_row": 1, "company": "Acme", "title": "Senior Engineer", "status": "applied",
        "url": "https://acme.com/jobs/1", "description": "Senior Engineer role at Acme. Requirements: Python.",
        "fit_score": "80",
    }
    base.update(overrides)
    return base


def _no_op_confirm(monkeypatch):
    monkeypatch.setattr(classifier, "confirm_company_name", lambda desc, guess: guess)


class TestCheckRowIdentity:
    def test_missing_title_short_circuits_other_checks(self, monkeypatch):
        problems = audit._check_row(_row(title=""))
        assert any("missing" in p for p in problems)
        assert len(problems) == 1  # nothing else checked once identity is broken

    def test_generic_listing_title_is_flagged(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(title="Open Positions"))
        assert any("generic listing label" in p for p in problems)


class TestCheckRowContent:
    def test_missing_url_and_description_both_flagged(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(url="", description=""))
        assert any("no url" in p for p in problems)
        assert any("no description" in p for p in problems)


class TestCheckRowCompany:
    def test_confirmed_company_mismatch_is_flagged(self, monkeypatch):
        monkeypatch.setattr(classifier, "confirm_company_name", lambda desc, guess: "BigBear.ai")
        problems = audit._check_row(_row(company="CargoSeer"))
        assert any("BigBear.ai" in p and "CargoSeer" in p for p in problems)

    def test_confirmed_company_match_is_not_flagged(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(company="Acme"))
        assert not any("company may be wrong" in p for p in problems)


class TestCheckRowScore:
    def test_missing_score_is_flagged(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(fit_score=""))
        assert any("no fit_score" in p for p in problems)

    def test_below_threshold_score_is_not_flagged(self, monkeypatch):
        # Real false positive found the first live run: backfill_career_links.py
        # scores a row *after* a real application/reply already happened (never
        # gated), and HARD_REQUIREMENT_SCORE_CAP legitimately produces low scores
        # by design -- this script can't tell either apart from "gated at
        # insertion" from a flat sheet row, so it doesn't guess.
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(fit_score="20"))
        assert not any("FIT_SCORE_THRESHOLD" in p for p in problems)

    def test_out_of_range_score_is_flagged(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(fit_score="150"))
        assert any("out of 0-100 range" in p for p in problems)

    def test_non_numeric_score_is_flagged(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        problems = audit._check_row(_row(fit_score="not-a-number"))
        assert any("isn't a valid number" in p for p in problems)


class TestLivenessScope:
    """Liveness only matters if you haven't already applied -- once applied,
    whether the original posting is still accepting new applicants is irrelevant."""

    def test_applied_row_never_triggers_a_liveness_fetch(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        called = {}
        monkeypatch.setattr(job_page_fetcher, "fetch_generic_posting", lambda url: called.setdefault("yes", True))
        audit._check_row(_row(status="applied"))
        assert "yes" not in called

    def test_not_applied_yet_row_does_trigger_a_liveness_fetch(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        called = {}

        def _fetch(url):
            called["yes"] = True
            return job_page_fetcher.FetchedPosting(description="still live content", closed=False)

        monkeypatch.setattr(job_page_fetcher, "fetch_generic_posting", _fetch)
        audit._check_row(_row(status="not applied yet"))
        assert called.get("yes") is True

    def test_closed_link_is_flagged_for_a_not_applied_row(self, monkeypatch):
        _no_op_confirm(monkeypatch)
        monkeypatch.setattr(job_page_fetcher, "fetch_generic_posting",
                             lambda url: job_page_fetcher.FetchedPosting(description="x", closed=True))
        problems = audit._check_row(_row(status="not applied yet"))
        assert any("no longer accepting applications" in p for p in problems)
