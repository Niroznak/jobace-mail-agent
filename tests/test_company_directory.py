"""Tests for company_directory.py's CSV read/write and search-caching logic.

Network calls (job_page_fetcher.search_duckduckgo, is_link_usable) are mocked --
these tests verify the caching/state-machine logic, not real web search.
"""
from mail_agent import company_directory as cd
from mail_agent import job_page_fetcher


def use_temp_csv(tmp_path, monkeypatch):
    csv_path = tmp_path / "tracked_companies.csv"
    monkeypatch.setattr(cd.config, "TRACKED_COMPANIES_CSV", str(csv_path))
    return csv_path


class TestCsvRoundTrip:
    def test_append_and_find(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Acme", "", "https://acme.example/careers")
        row = cd.find("Acme")
        assert row["Link"] == "https://acme.example/careers"

    def test_find_is_case_insensitive(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Acme Corp", "", "https://acme.example")
        assert cd.find("acme corp") is not None

    def test_update_link_clears_stale_no_link_marker(self, tmp_path, monkeypatch):
        # Real behavior: the user manually adds a link after a [NEEDS CSV ENTRY]
        # alert, but might leave the old "no link found (auto)" note in place --
        # update_link must clear it so the CSV doesn't look contradictory.
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Acme", "no link found (auto)", "")
        cd.update_link("Acme", "https://acme.example/careers")
        row = cd.find("Acme")
        assert row["Link"] == "https://acme.example/careers"
        assert row["Notes"] == ""

    def test_update_notes_preserves_link(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Acme", "", "https://acme.example")
        cd.update_notes("Acme", "no link found (auto)")
        row = cd.find("Acme")
        assert row["Link"] == "https://acme.example"
        assert row["Notes"] == "no link found (auto)"


class TestGetCareerLinkSearchIsOneTimeOnly:
    def test_search_only_invoked_once_for_unresolvable_company(self, tmp_path, monkeypatch):
        # Real bug this guards against: DuckDuckGo scraping is CAPTCHA-blocked, and
        # a company with a permanently-blank link used to get re-searched (and
        # re-fail) on every single scheduled run.
        use_temp_csv(tmp_path, monkeypatch)
        call_count = {"n": 0}

        def fake_discover(company):
            call_count["n"] += 1
            return None

        monkeypatch.setattr(cd, "discover_career_link", fake_discover)
        monkeypatch.setattr(cd, "_alert_no_link_found", lambda company: None)

        link1, notes1 = cd.get_career_link("Unfindable Co")
        assert link1 is None
        assert call_count["n"] == 1

        link2, notes2 = cd.get_career_link("Unfindable Co")
        assert link2 is None
        assert call_count["n"] == 1  # NOT called again

    def test_disqualified_company_never_triggers_discovery(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("BadCo", "disqualified", "")

        def fail_if_called(company):
            raise AssertionError("discover_career_link should never be called for a disqualified company")

        monkeypatch.setattr(cd, "discover_career_link", fail_if_called)
        link, notes = cd.get_career_link("BadCo")
        assert link is None
        assert "disqualified" in notes.lower()

    def test_existing_usable_link_skips_discovery(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("GoodCo", "", "https://goodco.example/careers")
        monkeypatch.setattr(cd, "is_link_usable", lambda link, company: True)

        def fail_if_called(company):
            raise AssertionError("discover_career_link should not be called when the CSV link is usable")

        monkeypatch.setattr(cd, "discover_career_link", fail_if_called)
        link, notes = cd.get_career_link("GoodCo")
        assert link == "https://goodco.example/careers"

    def test_dead_link_triggers_one_rediscovery_attempt(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("StaleCo", "", "https://dead-link.example")
        monkeypatch.setattr(cd, "is_link_usable", lambda link, company: link != "https://dead-link.example")
        monkeypatch.setattr(cd, "discover_career_link", lambda company: "https://fresh-link.example")

        link, notes = cd.get_career_link("StaleCo")
        assert link == "https://fresh-link.example"
        # repaired in place, not duplicated
        assert cd.find("StaleCo")["Link"] == "https://fresh-link.example"


class TestPickFromResultsRejectsNonCareerUrls:
    """Real incident: the text-match fallback accepted a third-party company-profile
    site (kmeans.io/company/mentee-robotics -- not the company's own career page, and
    it 402'd on fetch) purely because its title/snippet mentioned the company name --
    which the query "<company> careers" makes true of almost any result."""

    def _result(self, url, title, snippet):
        return job_page_fetcher.SearchResult(url=url, title=title, snippet=snippet)

    def test_rejects_a_company_profile_site_with_no_career_url_markers(self, monkeypatch):
        monkeypatch.setattr(cd, "is_link_usable", lambda url, company: False)
        results = [self._result(
            "https://www.kmeans.io/company/mentee-robotics",
            "Mentee Robotics - Company Profile", "Mentee Robotics careers and funding info",
        )]
        assert cd._pick_from_results(results, "Mentee Robotics") is None

    def test_accepts_unfetchable_page_when_url_looks_like_a_real_career_page(self, monkeypatch):
        # The case this fallback exists for: a real JS-rendered career page that a
        # plain fetch can't read, but whose URL is unambiguously a careers page.
        monkeypatch.setattr(cd, "is_link_usable", lambda url, company: False)
        results = [self._result(
            "https://career.example.com/careers/mentee-robotics",
            "Mentee Robotics Careers", "Join the Mentee Robotics team",
        )]
        assert cd._pick_from_results(results, "Mentee Robotics") == "https://career.example.com/careers/mentee-robotics"

    def test_known_ats_domain_counts_as_a_career_url(self, monkeypatch):
        monkeypatch.setattr(cd, "is_link_usable", lambda url, company: False)
        results = [self._result(
            "https://boards.greenhouse.io/menteerobotics",
            "Mentee Robotics", "Open positions at Mentee Robotics",
        )]
        assert cd._pick_from_results(results, "Mentee Robotics") is not None

    def test_a_confirmed_fetch_still_wins_regardless_of_url_shape(self, monkeypatch):
        monkeypatch.setattr(cd, "is_link_usable", lambda url, company: True)
        results = [self._result("https://example.com/anything", "t", "s")]
        assert cd._pick_from_results(results, "Acme") == "https://example.com/anything"


class TestIsLinkUsableRequiresCareerUrl:
    """Real incident: "Ethosia" -> a news article on calcalistech.com that merely
    mentioned the company by name passed the old check (company name found in fetched
    content) even though it's not a career page at all."""

    def test_rejects_content_match_on_a_non_career_url(self, monkeypatch):
        monkeypatch.setattr(job_page_fetcher, "fetch_generic_posting",
                             lambda url: job_page_fetcher.FetchedPosting(description="All about Ethosia, a great startup."))
        assert not cd.is_link_usable("https://www.calcalistech.com/ctechnews/article/sk7fnlaa2", "Ethosia")

    def test_accepts_content_match_on_a_real_career_url(self, monkeypatch):
        monkeypatch.setattr(job_page_fetcher, "fetch_generic_posting",
                             lambda url: job_page_fetcher.FetchedPosting(description="Join the Ethosia team. Open roles below."))
        assert cd.is_link_usable("https://ethosia.com/careers", "Ethosia")

    def test_blank_link_is_never_usable(self):
        assert not cd.is_link_usable("", "Ethosia")
