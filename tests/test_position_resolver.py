"""Tests for position_resolver.resolve_from_page_text -- the human-opens-link path.
No network/Ollama calls: classifier.confirm_company_name, score.score_position, and
reconcile.reconcile are all mocked. These tests verify resolve_from_page_text wires
them together correctly, not the stages themselves (already tested elsewhere).
"""
from mail_agent import classifier, position_resolver as pr
from mail_agent.pipeline import reconcile, score
from mail_agent.pipeline.types import ReconcileResult, ScoredItem, VerifiedPosition


class TestResolveFromPageText:
    def test_page_text_too_short_is_low_fit_without_scoring(self, monkeypatch):
        called = []
        monkeypatch.setattr(score, "score_position", lambda v: called.append(v))
        result = pr.resolve_from_page_text("Acme", "Engineer", "https://x.com/job/1", "short", [], None)
        assert result.action == "low_fit"
        assert called == []  # never even tries to score near-empty text

    def test_below_threshold_returns_low_fit_and_never_reconciles(self, monkeypatch):
        monkeypatch.setattr(classifier, "confirm_company_name", lambda text, guess: guess)
        monkeypatch.setattr(score, "score_position", lambda v: None)  # score.py's own below-threshold return
        called = []
        monkeypatch.setattr(reconcile, "reconcile", lambda *a, **k: called.append(1))
        result = pr.resolve_from_page_text("Acme", "Engineer", "https://x.com/job/1", "x" * 200, [], None)
        assert result.action == "low_fit"
        assert called == []

    def test_match_or_above_threshold_flows_into_reconcile(self, monkeypatch):
        monkeypatch.setattr(classifier, "confirm_company_name", lambda text, guess: "Confirmed Co")
        fake_scored = object()
        monkeypatch.setattr(score, "score_position", lambda v: fake_scored)
        captured = {}
        monkeypatch.setattr(reconcile, "reconcile", lambda item, rows, sheets, dry_run: captured.update(
            item=item, rows=rows, sheets=sheets, dry_run=dry_run) or ReconcileResult(action="inserted", row_number=42, detail="score=80"))

        result = pr.resolve_from_page_text("Acme Guess", "Engineer", "https://x.com/job/1", "x" * 200, ["row"], "sheets-obj")

        assert result.action == "inserted" and result.row_number == 42
        assert captured["item"] is fake_scored
        assert captured["rows"] == ["row"] and captured["sheets"] == "sheets-obj" and captured["dry_run"] is False

    def test_company_name_is_confirmed_against_the_real_page_text(self, monkeypatch):
        confirmed_with = {}

        def fake_confirm(text, guess):
            confirmed_with["args"] = (text, guess)
            return "Real Co"

        monkeypatch.setattr(classifier, "confirm_company_name", fake_confirm)
        captured_verified = {}

        def fake_score(verified):
            captured_verified["v"] = verified
            return None

        monkeypatch.setattr(score, "score_position", fake_score)
        pr.resolve_from_page_text("Guessed Co", "Engineer", "https://x.com/job/1", "real page text " * 20, [], None)

        assert confirmed_with["args"][1] == "Guessed Co"
        assert captured_verified["v"].company == "Real Co"

    def test_synthetic_candidate_is_marked_manual_browser_source(self, monkeypatch):
        monkeypatch.setattr(classifier, "confirm_company_name", lambda text, guess: guess)
        captured = {}

        def fake_score(verified):
            captured["v"] = verified
            return None

        monkeypatch.setattr(score, "score_position", fake_score)
        pr.resolve_from_page_text("Acme", "Engineer", "https://x.com/job/1", "x" * 200, [], None)

        assert captured["v"].candidate.source == "manual_browser"
        assert captured["v"].candidate.kind == "opportunity"
        assert captured["v"].url == "https://x.com/job/1"

    def test_dry_run_is_passed_through_to_reconcile(self, monkeypatch):
        monkeypatch.setattr(classifier, "confirm_company_name", lambda text, guess: guess)
        monkeypatch.setattr(score, "score_position", lambda v: object())
        captured = {}
        monkeypatch.setattr(reconcile, "reconcile", lambda item, rows, sheets, dry_run: captured.update(dry_run=dry_run) or ReconcileResult(action="inserted", row_number=1, detail=""))
        pr.resolve_from_page_text("Acme", "Engineer", "https://x.com/job/1", "x" * 200, [], None, dry_run=True)
        assert captured["dry_run"] is True


class TestTitlesMatch:
    def test_exact_title_matches(self):
        assert pr._titles_match("Senior ML Engineer", "Senior ML Engineer")

    def test_reordered_or_suffixed_title_matches(self):
        assert pr._titles_match("Senior ML Engineer", "Senior ML Engineer - Dallas, TX")
        assert pr._titles_match("Senior ML & LLM Platform Engineer", "Senior ML and LLM Platform Engineer, Remote")

    def test_missing_a_significant_word_does_not_match(self):
        assert not pr._titles_match("Senior ML Platform Engineer", "Senior ML Engineer")

    def test_single_common_word_never_matches(self):
        # Real risk this guards against: "Engineer" alone would match almost anything.
        assert not pr._titles_match("Engineer", "Senior Software Engineer")

    def test_completely_different_titles_do_not_match(self):
        assert not pr._titles_match("Senior ML Engineer", "Marketing Coordinator")


class TestFindOnCareerPage:
    def test_no_career_link_returns_empty(self, monkeypatch):
        from mail_agent import company_directory
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: (None, "no link found (auto)"))
        result = pr.find_on_career_page("Acme", "Senior ML Engineer")
        assert result.url == "" and result.description == ""

    def test_disqualified_company_returns_empty(self, monkeypatch):
        from mail_agent import company_directory
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: ("https://acme.example/careers", "disqualified"))
        called = []
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_raw_html", lambda url: called.append(url))
        result = pr.find_on_career_page("Acme", "Senior ML Engineer")
        assert result.url == "" and called == []

    def test_no_job_links_on_page_returns_empty(self, monkeypatch):
        from mail_agent import company_directory
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: ("https://acme.example/careers", ""))
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_raw_html", lambda url: "<html></html>")
        monkeypatch.setattr(pr.job_page_fetcher, "extract_job_links", lambda html, base: [])
        result = pr.find_on_career_page("Acme", "Senior ML Engineer")
        assert result.url == ""

    def test_matching_title_is_fetched_and_returned(self, monkeypatch):
        from mail_agent import company_directory
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: ("https://acme.example/careers", ""))
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_raw_html", lambda url: "<html>...</html>")
        monkeypatch.setattr(pr.job_page_fetcher, "extract_job_links", lambda html, base: [
            ("Junior Analyst", "https://acme.example/job/1"),
            ("Senior ML Engineer, Remote", "https://acme.example/job/2"),
        ])
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_generic_posting",
                             lambda url: pr.job_page_fetcher.FetchedPosting(description="Real job description text here."))
        monkeypatch.setattr(classifier, "confirm_company_name", lambda text, guess: guess)

        result = pr.find_on_career_page("Acme", "Senior ML Engineer")
        assert result.url == "https://acme.example/job/2"
        assert result.description == "Real job description text here."

    def test_no_matching_title_returns_empty_not_a_guess(self, monkeypatch):
        from mail_agent import company_directory
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: ("https://acme.example/careers", ""))
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_raw_html", lambda url: "<html>...</html>")
        monkeypatch.setattr(pr.job_page_fetcher, "extract_job_links", lambda html, base: [("Completely Different Role", "https://acme.example/job/9")])
        called = []
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_generic_posting", lambda url: called.append(url))
        result = pr.find_on_career_page("Acme", "Senior ML Engineer")
        assert result.url == "" and called == []  # never fetches a non-matching candidate

    def test_closed_matched_posting_returns_empty(self, monkeypatch):
        from mail_agent import company_directory
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: ("https://acme.example/careers", ""))
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_raw_html", lambda url: "<html>...</html>")
        monkeypatch.setattr(pr.job_page_fetcher, "extract_job_links", lambda html, base: [("Senior ML Engineer", "https://acme.example/job/2")])
        monkeypatch.setattr(pr.job_page_fetcher, "fetch_generic_posting",
                             lambda url: pr.job_page_fetcher.FetchedPosting(description="text", closed=True))
        result = pr.find_on_career_page("Acme", "Senior ML Engineer")
        assert result.url == ""
