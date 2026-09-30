"""Tests for pipeline.verify (stage 3) -- mocks classifier filters, sheets_client
dedup lookups, job_page_fetcher, and position_resolver; no network/Ollama calls.
"""
from mail_agent import classifier
from mail_agent import company_directory
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
        long_snippet = "Real job description. " * (verify.MIN_DIGEST_DESCRIPTION_CHARS // 20 + 2)
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
        monkeypatch.setattr(verify.state, "log_dropped_verification", lambda *a, **k: None)
        monkeypatch.setattr(verify.notifier, "notify_needs_review", lambda *a, **k: None)

        retry_state: dict = {}
        candidate = _candidate(source="single_email")
        for _ in range(config.MAX_RESOLUTION_ATTEMPTS):
            verify.verify_position(candidate, [], retry_state)
        # Exhausted -- entry must be gone, never lingering as a permanent stub.
        assert retry_state == {}

    def test_dropped_after_max_attempts_is_surfaced_not_silent(self, monkeypatch):
        # Real incident: a legitimate JLL posting was silently dropped after 3 failed
        # verification attempts and only noticed because the user was watching the
        # terminal at that exact moment. A drop must now be logged AND notified.
        from mail_agent import config
        _allow_all_filters(monkeypatch)
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(position_resolver, "resolve_position", lambda company, title, content: position_resolver.ResolvedPosition())
        logged, notified = {}, {}
        monkeypatch.setattr(verify.state, "log_dropped_verification", lambda *a, **k: logged.update(args=a))
        monkeypatch.setattr(verify.notifier, "notify_needs_review", lambda msg: notified.update(msg=msg))

        retry_state: dict = {}
        candidate = _candidate(source="single_email", company="JLL", title="Senior ML & LLM Platform Engineer")
        for _ in range(config.MAX_RESOLUTION_ATTEMPTS):
            verify.verify_position(candidate, [], retry_state)
        assert logged["args"][:2] == ("JLL", "Senior ML & LLM Platform Engineer")
        assert "JLL" in notified["msg"] and "dropped_verification.csv" in notified["msg"]

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


def test_teaser_length_snippet_is_not_scored(monkeypatch):
    # Indeed teasers are ~160 chars and the page fetch is blocked -- must not be scored.
    monkeypatch.setattr(verify.job_page_fetcher, "fetch_generic_posting", lambda url: verify.job_page_fetcher.FetchedPosting())
    result = verify.verify_position(_candidate(source="generic_digest", snippet="x" * 160, url="https://il.indeed.com/rc/clk/dl?jk=1"), [], {})
    assert result is None


class TestEnsureCompanyTracked:
    """Real motivation: LinkedIn/Indeed digests name companies whose SPECIFIC posting
    we currently can't fetch (platform blocks), but the company NAME itself is free --
    auto-discovering and caching that company's career page grows
    scan_career_pages.py's coverage independent of whether this candidate's own
    posting ever verifies."""

    def test_looks_up_and_caches_a_company_not_yet_tracked(self, monkeypatch):
        from mail_agent import config
        monkeypatch.setattr(config, "AUTO_DISCOVER_CAREER_PAGES", True)
        monkeypatch.setattr(company_directory, "find", lambda c: None)
        called = {}
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: called.setdefault("company", c))
        verify._ensure_company_tracked("New Company")
        assert called["company"] == "New Company"

    def test_does_not_re_lookup_an_already_tracked_company(self, monkeypatch):
        from mail_agent import config
        monkeypatch.setattr(config, "AUTO_DISCOVER_CAREER_PAGES", True)
        monkeypatch.setattr(company_directory, "find", lambda c: {"Company Name": c})
        called = []
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: called.append(c))
        verify._ensure_company_tracked("Already Tracked Co")
        assert called == []

    def test_disabled_by_config_does_nothing(self, monkeypatch):
        from mail_agent import config
        monkeypatch.setattr(config, "AUTO_DISCOVER_CAREER_PAGES", False)
        called = []
        monkeypatch.setattr(company_directory, "find", lambda c: (_ for _ in ()).throw(AssertionError("should not be called")))
        monkeypatch.setattr(company_directory, "get_career_link", lambda c: called.append(c))
        verify._ensure_company_tracked("Some Co")
        assert called == []

    def test_a_lookup_failure_never_raises(self, monkeypatch):
        from mail_agent import config
        monkeypatch.setattr(config, "AUTO_DISCOVER_CAREER_PAGES", True)
        monkeypatch.setattr(company_directory, "find", lambda c: None)
        def _raise(c):
            raise RuntimeError("search API down")
        monkeypatch.setattr(company_directory, "get_career_link", _raise)
        verify._ensure_company_tracked("Flaky Co")  # must not raise

    def test_blank_company_is_skipped(self, monkeypatch):
        from mail_agent import config
        monkeypatch.setattr(config, "AUTO_DISCOVER_CAREER_PAGES", True)
        monkeypatch.setattr(company_directory, "find", lambda c: (_ for _ in ()).throw(AssertionError("should not be called")))
        verify._ensure_company_tracked("   ")

    def test_verify_position_calls_ensure_company_tracked(self, monkeypatch):
        _allow_all_filters(monkeypatch)
        called = []
        monkeypatch.setattr(verify, "_ensure_company_tracked", lambda c: called.append(c))
        monkeypatch.setattr(sheets_client, "active_rows", lambda rows: rows)
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(job_page_fetcher, "fetch_linkedin_posting", lambda url: job_page_fetcher.FetchedPosting())
        verify.verify_position(_candidate(source="linkedin_digest", company="Some New Co"), [], {})
        assert called == ["Some New Co"]
