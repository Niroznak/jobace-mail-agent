"""Tests for check_source_availability.py's classification logic -- no real network
calls (job_page_fetcher.fetch_raw_html/extract_job_links are mocked)."""
import check_source_availability as csa
from mail_agent import company_directory as cd
from mail_agent import job_page_fetcher


def use_temp_csv(tmp_path, monkeypatch):
    csv_path = tmp_path / "tracked_companies.csv"
    monkeypatch.setattr(cd.config, "TRACKED_COMPANIES_CSV", str(csv_path))
    return csv_path


class TestCheckPlatformActiveProbe:
    def test_reachable_probe_reports_ok(self, monkeypatch):
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", lambda url: "<html>ok</html>")
        source = csa.PlatformSource("linkedin", "active_probe", probe_url="https://www.linkedin.com/jobs/search/")
        result = csa.check_platform(source)
        assert result["status"] == "OK"

    def test_blocked_probe_reports_blocked(self, monkeypatch):
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", lambda url: None)
        source = csa.PlatformSource("linkedin", "active_probe", probe_url="https://www.linkedin.com/jobs/search/")
        result = csa.check_platform(source)
        assert result["status"] == "BLOCKED"


class TestCheckPlatformRecentRealUrl:
    def test_no_recent_url_reports_untested_not_a_guess(self, monkeypatch, tmp_path):
        monkeypatch.setattr(csa, "_find_recent_real_url", lambda platform, **k: None)
        called = []
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", lambda url: called.append(url))
        result = csa.check_platform(csa.PlatformSource("indeed", "recent_real_url"))
        assert result["status"] == "UNTESTED"
        assert called == []  # never fabricates a URL to test against

    def test_recent_url_found_and_reachable_reports_ok(self, monkeypatch):
        monkeypatch.setattr(csa, "_find_recent_real_url", lambda platform, **k: "https://il.indeed.com/rc/clk/dl?jk=1")
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", lambda url: "<html>ok</html>")
        result = csa.check_platform(csa.PlatformSource("indeed", "recent_real_url"))
        assert result["status"] == "OK"

    def test_recent_url_found_but_blocked_reports_blocked(self, monkeypatch):
        monkeypatch.setattr(csa, "_find_recent_real_url", lambda platform, **k: "https://il.indeed.com/rc/clk/dl?jk=1")
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", lambda url: None)
        result = csa.check_platform(csa.PlatformSource("indeed", "recent_real_url"))
        assert result["status"] == "BLOCKED"


class TestFindRecentRealUrl:
    def test_finds_a_matching_url_from_dropped_verification_csv(self, tmp_path, monkeypatch):
        import csv as csv_module
        from datetime import date
        path = tmp_path / "dropped.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv_module.DictWriter(f, fieldnames=["date", "company", "title", "url", "reason", "attempts"])
            w.writeheader()
            w.writerow({
                "date": date.today().isoformat(), "company": "VAST Data", "title": "SWE",
                "url": "https://cts.indeed.com/v3/abc", "reason": "x", "attempts": 2,
            })
        monkeypatch.setattr(csa.config, "DROPPED_VERIFICATION_PATH", str(path))
        monkeypatch.setattr(csa.config, "LOGS_DIR", str(tmp_path / "no_logs"))
        url = csa._find_recent_real_url("indeed")
        assert url == "https://cts.indeed.com/v3/abc"

    def test_ignores_old_entries_past_the_age_cutoff(self, tmp_path, monkeypatch):
        import csv as csv_module
        path = tmp_path / "dropped.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv_module.DictWriter(f, fieldnames=["date", "company", "title", "url", "reason", "attempts"])
            w.writeheader()
            w.writerow({"date": "2020-01-01", "company": "Old Co", "title": "x",
                        "url": "https://cts.indeed.com/v3/old", "reason": "x", "attempts": 2})
        monkeypatch.setattr(csa.config, "DROPPED_VERIFICATION_PATH", str(path))
        monkeypatch.setattr(csa.config, "LOGS_DIR", str(tmp_path / "no_logs"))
        assert csa._find_recent_real_url("indeed") is None

    def test_no_file_at_all_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(csa.config, "DROPPED_VERIFICATION_PATH", str(tmp_path / "missing.csv"))
        monkeypatch.setattr(csa.config, "LOGS_DIR", str(tmp_path / "no_logs"))
        assert csa._find_recent_real_url("indeed") is None


class TestCheckDiscovery:
    def test_reports_key_missing(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        monkeypatch.setattr(csa.config, "SERPAPI_API_KEY", "")
        result = csa.check_discovery()
        assert result["status"] == "NOT CONFIGURED"

    def test_reports_key_configured_and_last_success(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Darrow AI", "", "https://www.darrow.ai/careers")
        monkeypatch.setattr(csa.config, "SERPAPI_API_KEY", "fake-key")
        result = csa.check_discovery()
        assert result["status"] == "OK" and "Darrow AI" in result["detail"]

    def test_no_link_found_marker_entries_are_not_counted_as_discovered(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Nowhere Co", cd.NO_LINK_FOUND_MARKER, "")
        monkeypatch.setattr(csa.config, "SERPAPI_API_KEY", "fake-key")
        result = csa.check_discovery()
        assert "Nowhere Co" not in result["detail"]


class TestCheckCompanySites:
    def test_classifies_reachable_with_jobs_vs_no_jobs_vs_unreachable(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("HasJobs", "", "https://hasjobs.example/careers")
        cd.append("NoJobsNow", "", "https://nojobs.example/careers")
        cd.append("Unreachable", "", "https://dead.example/careers")
        monkeypatch.setattr(csa.time, "sleep", lambda s: None)

        def fake_fetch(url):
            return None if "dead" in url else f"<html>{url}</html>"
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", fake_fetch)
        monkeypatch.setattr(job_page_fetcher, "extract_job_links",
                             lambda html, base: [("Role", "https://x/job/1")] if "hasjobs" in html else [])

        result = csa.check_company_sites()
        assert result["reachable_with_jobs"] == ["HasJobs"]
        assert result["reachable_no_jobs"] == ["NoJobsNow"]
        assert result["unreachable"] == ["Unreachable"]

    def test_skips_disqualified_and_no_link_found_rows(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Disqualified Co", "disqualified", "https://x.example/careers")
        cd.append("No Link Co", cd.NO_LINK_FOUND_MARKER, "")
        monkeypatch.setattr(csa.time, "sleep", lambda s: None)
        called = []
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", lambda url: called.append(url))

        result = csa.check_company_sites()
        assert result["total"] == 0 and called == []

    def test_fetch_exception_is_captured_not_raised(self, tmp_path, monkeypatch):
        use_temp_csv(tmp_path, monkeypatch)
        cd.append("Flaky", "", "https://flaky.example/careers")
        monkeypatch.setattr(csa.time, "sleep", lambda s: None)

        def _raise(url):
            raise RuntimeError("boom")
        monkeypatch.setattr(job_page_fetcher, "fetch_raw_html", _raise)

        result = csa.check_company_sites()
        assert result["errored"] == [("Flaky", "RuntimeError")]
