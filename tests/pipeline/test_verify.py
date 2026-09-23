"""Tests for pipeline.verify (stage 3) -- mocks classifier filters, sheets_client
dedup lookups, job_page_fetcher, and position_resolver; no network/Ollama calls.
"""
from mail_agent import classifier
from mail_agent import job_page_fetcher
from mail_agent import position_resolver
from mail_agent import sheets_client
from mail_agent.pipeline import verify
from mail_agent.pipeline.types import Candidate


def _candidate(**overrides) -> Candidate:
    base = dict(mail_id="m1", kind="opportunity", source="single_email", company="Acme", title="Engineer")
    base.update(overrides)
    return Candidate(**base)


def _allow_all_filters(monkeypatch):
    monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)
    monkeypatch.setattr(classifier, "is_junior_or_intern_title", lambda t: False)
    monkeypatch.setattr(classifier, "is_location_excluded", lambda loc: False)


class TestEarlyFilters:
    def test_platform_company_is_rejected(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: True)
        result = verify.verify_position(_candidate(company="LinkedIn"), [], {})
        assert result is None

    def test_generic_listing_title_is_rejected(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        result = verify.verify_position(_candidate(title="Open Positions"), [], {})
        assert result is None

    def test_junior_title_is_rejected(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)
        monkeypatch.setattr(classifier, "is_junior_or_intern_title", lambda t: True)
        result = verify.verify_position(_candidate(), [], {})
        assert result is None

    def test_excluded_location_is_rejected(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)
        monkeypatch.setattr(classifier, "is_junior_or_intern_title", lambda t: False)
        monkeypatch.setattr(classifier, "is_location_excluded", lambda loc: True)
        result = verify.verify_position(_candidate(location="Jerusalem"), [], {})
        assert result is None


class TestLinkedinDigestVerification:
    def test_duplicate_by_listing_id_is_rejected(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: {"_row": 1})
        result = verify.verify_position(_candidate(source="linkedin_digest", url="https://linkedin.com/x"), [], {})
        assert result is None

    def test_closed_posting_is_rejected(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(job_page_fetcher, "fetch_linkedin_posting", lambda url: job_page_fetcher.FetchedPosting(closed=True))
        result = verify.verify_position(_candidate(source="linkedin_digest", url="https://linkedin.com/x"), [], {})
        assert result is None

    def test_successful_fetch_returns_verified_position(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(sheets_client, "find_row_by_company_and_description", lambda rows, company, desc: None)
        monkeypatch.setattr(job_page_fetcher, "fetch_linkedin_posting",
                             lambda url: job_page_fetcher.FetchedPosting(description="real job description text", closed=False))
        monkeypatch.setattr(classifier, "extract_requisition_id", lambda desc: "")
        monkeypatch.setattr(classifier, "confirm_company_name", lambda desc, guess: guess)
        result = verify.verify_position(_candidate(source="linkedin_digest", url="https://linkedin.com/x"), [], {})
        assert result is not None
        assert result.description == "real job description text"

    def test_company_gets_corrected_against_real_page_text(self, monkeypatch):
        # Real incident: a LinkedIn digest listed "CargoSeer" as the company, but
        # the actual posting page explicitly said "...interviewing at BigBear.ai"
        # -- CargoSeer never appeared anywhere on the real page. The digest path
        # never verified this before; it now does, the same way the single-email
        # path already did via position_resolver.
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(sheets_client, "find_row_by_company_and_description", lambda rows, company, desc: None)
        monkeypatch.setattr(job_page_fetcher, "fetch_linkedin_posting",
                             lambda url: job_page_fetcher.FetchedPosting(description="...interviewing at BigBear.ai...", closed=False))
        monkeypatch.setattr(classifier, "extract_requisition_id", lambda desc: "")
        monkeypatch.setattr(classifier, "confirm_company_name", lambda desc, guess: "BigBear.ai")

        result = verify.verify_position(_candidate(source="linkedin_digest", company="CargoSeer", url="https://linkedin.com/x"), [], {})

        assert result is not None
        assert result.company == "BigBear.ai"


class TestGenericDigestVerification:
    def test_short_snippet_and_no_url_is_rejected(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        result = verify.verify_position(_candidate(source="generic_digest", snippet="too short"), [], {})
        assert result is None

    def test_long_enough_snippet_is_verified_without_fetching(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(classifier, "confirm_company_name", lambda desc, guess: guess)
        long_snippet = "Real job description. " * (position_resolver.MIN_CONTENT_LENGTH // 20 + 2)
        result = verify.verify_position(_candidate(source="generic_digest", snippet=long_snippet), [], {})
        assert result is not None


class TestRetryAndGiveUp:
    """Real behavior change: a freshly-discovered opportunity that never verifies
    is now dropped entirely after MAX_RESOLUTION_ATTEMPTS, tracked in a persisted
    retry_state dict since the triggering email is marked read either way."""

    def test_first_failure_is_persisted_for_retry(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(position_resolver, "resolve_position", lambda company, title, content: position_resolver.ResolvedPosition())

        retry_state: dict = {}
        result = verify.verify_position(_candidate(source="single_email"), [], retry_state)
        assert result is None
        assert len(retry_state) == 1
        entry = next(iter(retry_state.values()))
        assert entry["attempts"] == 1
        assert entry["candidate"]["company"] == "Acme"

    def test_gives_up_and_drops_after_max_attempts(self, monkeypatch):
        from mail_agent import config
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(position_resolver, "resolve_position", lambda company, title, content: position_resolver.ResolvedPosition())

        retry_state: dict = {}
        candidate = _candidate(source="single_email")
        for _ in range(config.MAX_RESOLUTION_ATTEMPTS):
            verify.verify_position(candidate, [], retry_state)
        # Exhausted -- entry must be gone, never lingering as a permanent stub.
        assert retry_state == {}

    def test_pending_candidates_reconstructs_from_state(self):
        candidate = _candidate(source="single_email", title="Reconstructed Role")
        retry_state = {"job1": {"attempts": 1, "candidate": {
            "mail_id": candidate.mail_id, "kind": candidate.kind, "source": candidate.source,
            "company": candidate.company, "title": candidate.title,
        }}}
        result = verify.pending_candidates(retry_state)
        assert len(result) == 1
        assert result[0].title == "Reconstructed Role"

    def test_success_clears_any_pending_retry_entry(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(
            position_resolver, "resolve_position",
            lambda company, title, content: position_resolver.ResolvedPosition(url="https://x", description="real desc", company=company),
        )
        candidate = _candidate(source="single_email")
        from mail_agent.pipeline import dedup
        jid = dedup.job_id_for(candidate.company, candidate.title, candidate.position_id)
        retry_state = {jid: {"attempts": 1, "candidate": {}}}

        result = verify.verify_position(candidate, [], retry_state)
        assert result is not None
        assert jid not in retry_state
