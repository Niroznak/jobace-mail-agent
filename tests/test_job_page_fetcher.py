"""Tests for the pure text-processing helpers in job_page_fetcher.py -- no network."""
from mail_agent import job_page_fetcher as jpf


class TestIsClosedPosting:
    def test_detects_common_closed_phrases(self):
        assert jpf.is_closed_posting("This job is no longer accepting applications.")
        assert jpf.is_closed_posting("Position has been filled.")

    def test_open_posting_not_flagged(self):
        assert not jpf.is_closed_posting("We are looking for a great engineer to join our team.")

    def test_blank_text_not_flagged(self):
        assert not jpf.is_closed_posting("")


class TestCleanHtml:
    def test_strips_tags(self):
        assert jpf._clean_html("<p>Hello</p>") == "Hello"

    def test_strips_script_and_style_content(self):
        # Real bug: a JS-rendered SPA career page's embedded JSON/i18n strings
        # leaked through as if they were visible page text (Similarweb careers page),
        # producing a false-positive title match against pure UI noise.
        html = '<html><script>{"nav.title":"Analysts"}</script><body>Real content</body></html>'
        result = jpf._clean_html(html)
        assert "nav.title" not in result
        assert "Analysts" not in result
        assert "Real content" in result

    def test_strips_style_content(self):
        html = "<style>.foo { color: red; }</style><p>Visible text</p>"
        result = jpf._clean_html(html)
        assert "color" not in result
        assert "Visible text" in result


class TestExtractSnippetNear:
    def test_finds_exact_title_phrase(self):
        text = "Careers page.\n\nJunior Backend Developer - apply now, great team.\n\nOther job."
        snippet = jpf.extract_snippet_near(text, "Junior Backend Developer")
        assert "Backend Developer" in snippet

    def test_rejects_single_word_coincidental_match(self):
        # Real bug: a career page's own nav chrome ("Research & Analysts" menu item)
        # coincidentally contained the single word "Analyst", causing a false-positive
        # match against a "...Analyst" title even though the real position wasn't
        # actually listed anywhere on the page.
        page_text = "Products Solutions Research & Analysts Pricing Login"
        snippet = jpf.extract_snippet_near(page_text, "ML & Big Data Analyst")
        assert snippet == ""

    def test_full_phrase_present_matches(self):
        page_text = "Open roles: ML & Big Data Analyst -- join our data team today."
        snippet = jpf.extract_snippet_near(page_text, "ML & Big Data Analyst")
        assert "ML" in snippet and "Analyst" in snippet

    def test_blank_title_returns_empty(self):
        assert jpf.extract_snippet_near("some page text", "") == ""

    def test_title_not_on_page_returns_empty(self):
        assert jpf.extract_snippet_near("completely unrelated page content here", "Senior Data Scientist") == ""


class TestLooksLikeJobPosting:
    def test_real_posting_text_passes(self):
        text = "About the role\nRequirements:\n- 5 years experience\nResponsibilities:\n- Build things"
        assert jpf.looks_like_job_posting(text)

    def test_nav_only_text_fails(self):
        assert not jpf.looks_like_job_posting("Home About Contact Products Careers Login")

    def test_json_dump_with_coincidental_label_fails(self):
        # Real bug: a Workday page's UI config literally contains
        # '"label": "Job Description"' as a field name, not real content -- a naive
        # word-based check alone would accept it.
        dump = '{&#34;label&#34;: &#34;Job Description&#34;, &#34;id&#34;: &#34;x&#34;}' * 50
        assert not jpf.looks_like_job_posting(dump)

    def test_blank_text_fails(self):
        assert not jpf.looks_like_job_posting("")

    def test_hebrew_posting_passes(self):
        # Real bug: an entirely-Hebrew posting (SCD's real "Data Engineer (JMP)"
        # listing -- דרישות = requirements, תחומי אחריות = responsibilities) failed
        # every signal word in the English-only list and got scored 0 as "not a
        # real job posting," despite being a genuine, detailed listing.
        text = "תיאור התפקיד\nתחומי אחריות עיקריים\n• ניתוח נתונים\nדרישות\n• תואר ראשון בסטטיסטיקה"
        assert jpf.looks_like_job_posting(text)


class TestExtractJobLinks:
    _SAMPLE_HTML = """
    <html><body>
    <nav><a href="/about">About Us</a><a href="/contact">Contact</a></nav>
    <a href="/job/12345">Senior Algorithm Engineer</a>
    <a href="/jobs/67890">Data Scientist, ML Platform</a>
    <a href="https://external-ats.example.com/job/999">Should Be Excluded (cross-domain)</a>
    <a href="/careers">All Jobs</a>
    <a href="/job/12345">Senior Algorithm Engineer</a>
    </body></html>
    """

    def test_extracts_job_shaped_same_domain_links(self):
        results = jpf.extract_job_links(self._SAMPLE_HTML, "https://example.com/careers")
        urls = [url for _, url in results]
        assert "https://example.com/job/12345" in urls
        assert "https://example.com/jobs/67890" in urls

    def test_excludes_cross_domain_links(self):
        results = jpf.extract_job_links(self._SAMPLE_HTML, "https://example.com/careers")
        assert not any("external-ats.example.com" in url for _, url in results)

    def test_excludes_nav_noise(self):
        results = jpf.extract_job_links(self._SAMPLE_HTML, "https://example.com/careers")
        titles = [title.lower() for title, _ in results]
        assert "about us" not in titles
        assert "contact" not in titles
        assert "all jobs" not in titles

    def test_dedups_repeated_links(self):
        results = jpf.extract_job_links(self._SAMPLE_HTML, "https://example.com/careers")
        urls = [url for _, url in results]
        assert urls.count("https://example.com/job/12345") == 1

    def test_rejects_generic_cta_link_text(self):
        # Real bug: Camtek's career page uses a "Read More >" (HTML-entity undecoded:
        # "Read More &gt;") link per job card, with the real title in a separate
        # heading element this anchor-only heuristic can't see. Each card's distinct
        # URL made it look like N different "new" postings, all with the same
        # meaningless "title" -- each got individually (and expensively) LLM-scored
        # with wildly inconsistent results before dry-run caught it pre-write.
        html = """
        <a href="/job/1">Read More &gt;</a>
        <a href="/job/2">read more</a>
        <a href="/job/3">Apply Now</a>
        <a href="/job/4">View Details</a>
        """
        assert jpf.extract_job_links(html, "https://example.com/careers") == []

    def test_rejects_product_page_with_incidental_digits_in_slug(self):
        # Real bug: a "3+ digits anywhere in the path" fallback (since removed)
        # matched "402" inside a product page's model-number slug
        # ("/products/ds402-ethercat-servo-drives/"), letting it through as a fake
        # job candidate -- it then got scored against nav-chrome/product text and
        # written to the sheet as a fabricated "job" (ACS Motion Control, 2026-09-18).
        html = '<a href="/products/ds402-ethercat-servo-drives/">Intelligent Drive Modules</a>'
        assert jpf.extract_job_links(html, "https://example.com/careers") == []

    def test_accepts_ats_embedded_link_without_job_keyword(self):
        # Comeet-hosted job links use a numeric position code, not a job-related
        # keyword, in the path -- must still be recognized via the ATS-platform marker.
        html = '<a href="/comeet/co/acme-hq/68.964/npi-project-manager/all">NPI Project Manager</a>'
        results = jpf.extract_job_links(html, "https://example.com/careers")
        assert ("NPI Project Manager", "https://example.com/comeet/co/acme-hq/68.964/npi-project-manager/all") in results

    def test_ignores_links_inside_shared_site_nav(self):
        # Real bug: a career page's shared site-wide <nav> (present on every page of
        # the domain, not just the careers page) contained an ordinary "Products"
        # menu item that passed every other check -- same domain, real page,
        # plausible-length title -- and got written to the sheet as a fabricated job
        # (ACS Motion Control, 2026-09-18: "Intelligent Drive Modules" -> a product
        # page, matched via the site's own <nav> menu, not any job listing).
        html = """
        <header><nav>
          <a href="/products/ds402-ethercat-servo-drives/">Intelligent Drive Modules</a>
        </nav></header>
        <main>
          <a href="/comeet/co/acme/68.964/npi-project-manager/all">NPI Project Manager</a>
        </main>
        <footer><a href="/comeet/co/acme/99.111/legal-notice-role/all">Should Also Be Excluded</a></footer>
        """
        results = jpf.extract_job_links(html, "https://example.com/careers")
        titles = [t for t, _ in results]
        assert "Intelligent Drive Modules" not in titles
        assert "Should Also Be Excluded" not in titles
        assert "NPI Project Manager" in titles

    def test_js_rendered_empty_shell_yields_no_candidates(self):
        # Real case: career.rafael.co.il serves an empty <body> with only a bootstrap
        # script to a plain fetch -- extraction must fail closed (empty list), not
        # invent candidates from a page with no real links.
        html = '<!DOCTYPE html><html><head><script src="/app.js"></script></head><body></body></html>'
        assert jpf.extract_job_links(html, "https://career.rafael.co.il/") == []


class TestPlatformRateLimitCircuitBreaker:
    """Real incidents: (1) a single link-liveness sweep hit HTTP 429/999 (LinkedIn's
    bot-detected code) 22 times in one run -- every remaining LinkedIn URL still
    burned attempts/delays on a fetch already guaranteed to fail. (2) Indeed's
    tracking links return a flat 403 on EVERY candidate, every run, with no breaker
    at all -- generalized from LinkedIn-only to any known platform (classify_platform),
    never applied to "other" (a random company's own site)."""

    def setup_method(self):
        jpf._platform_blocked_until.clear()  # each test starts with a clean breaker

    teardown_method = setup_method

    def _raise_http_error(self, code):
        import urllib.error

        def _urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "blocked", {}, None)
        return _urlopen

    def test_429_trips_the_linkedin_breaker(self, monkeypatch):
        monkeypatch.setattr(jpf.urllib.request, "urlopen", self._raise_http_error(429))
        assert jpf._fetch_html("https://www.linkedin.com/jobs/view/123/") is None
        assert jpf.platform_rate_limited("https://www.linkedin.com/jobs/view/456/") is True

    def test_999_trips_the_linkedin_breaker(self, monkeypatch):
        monkeypatch.setattr(jpf.urllib.request, "urlopen", self._raise_http_error(999))
        assert jpf._fetch_html("https://www.linkedin.com/jobs/view/123/") is None
        assert jpf.platform_rate_limited("https://www.linkedin.com/jobs/view/456/") is True

    def test_403_trips_the_indeed_breaker(self, monkeypatch):
        # Real incident: every single Indeed tracking-link fetch returns 403 -- this
        # status code must trip Indeed's breaker even though it never trips LinkedIn's
        # (LinkedIn's own block code is 429/999, not 403).
        monkeypatch.setattr(jpf.urllib.request, "urlopen", self._raise_http_error(403))
        assert jpf._fetch_html("https://il.indeed.com/rc/clk/dl?jk=1") is None
        assert jpf.platform_rate_limited("https://il.indeed.com/rc/clk/dl?jk=2") is True

    def test_tripping_indeed_does_not_affect_linkedin_and_vice_versa(self, monkeypatch):
        monkeypatch.setattr(jpf.urllib.request, "urlopen", self._raise_http_error(403))
        jpf._fetch_html("https://il.indeed.com/rc/clk/dl?jk=1")
        assert jpf.platform_rate_limited("https://www.linkedin.com/jobs/view/1/") is False

    def test_non_platform_host_never_trips_any_breaker(self, monkeypatch):
        # A random company's own career site returning 403 for its own reasons must
        # never be treated as a "platform-wide" block -- that's only tracked for
        # recognized job-board platforms.
        monkeypatch.setattr(jpf.urllib.request, "urlopen", self._raise_http_error(403))
        assert jpf._fetch_html("https://example.com/careers/job/123") is None
        assert jpf.platform_rate_limited("https://example.com/careers/job/999") is False

    def test_unrelated_status_codes_do_not_trip_the_breaker(self, monkeypatch):
        monkeypatch.setattr(jpf.urllib.request, "urlopen", self._raise_http_error(404))
        assert jpf._fetch_html("https://www.linkedin.com/jobs/view/123/") is None
        assert jpf.platform_rate_limited("https://www.linkedin.com/jobs/view/456/") is False

    def test_tripped_linkedin_breaker_skips_fetch_entirely_with_no_network_call(self, monkeypatch):
        jpf._platform_blocked_until["linkedin"] = jpf.time.time() + 900
        called = []
        monkeypatch.setattr(jpf.urllib.request, "urlopen", lambda *a, **k: called.append(1))
        result = jpf.fetch_linkedin_posting("https://www.linkedin.com/comm/jobs/view/123/?trackingId=x")
        assert result.description == "" and result.closed is False
        assert called == []  # no network call was made at all

    def test_tripped_indeed_breaker_skips_generic_fetch_entirely(self, monkeypatch):
        jpf._platform_blocked_until["indeed"] = jpf.time.time() + 900
        called = []
        monkeypatch.setattr(jpf.urllib.request, "urlopen", lambda *a, **k: called.append(1))
        result = jpf.fetch_generic_posting("https://il.indeed.com/rc/clk/dl?jk=1")
        assert result.description == "" and called == []

    def test_generic_fetch_of_a_non_platform_site_is_unaffected_by_any_breaker(self, monkeypatch):
        jpf._platform_blocked_until["indeed"] = jpf.time.time() + 900
        jpf._platform_blocked_until["linkedin"] = jpf.time.time() + 900
        monkeypatch.setattr(jpf, "_fetch_html", lambda url: "<html>Job description text here</html>")
        result = jpf.fetch_generic_posting("https://example.com/careers/job/123")
        assert result.description != ""


class TestShortUrl:
    def test_strips_query_string_and_keeps_host_path(self):
        url = "https://www.linkedin.com/comm/jobs/view/4445912160/?trackingId=abc123&refId=xyz789&lipi=urn%3Ali"
        assert jpf.short_url(url) == "www.linkedin.com/comm/jobs/view/4445912160/"

    def test_truncates_pathologically_long_paths(self):
        url = "https://example.com/" + ("a" * 200)
        result = jpf.short_url(url)
        assert len(result) <= jpf._SHORT_URL_MAX_LEN and result.endswith("...")

    def test_empty_url_returns_empty(self):
        assert jpf.short_url("") == ""


class TestClassifyPlatform:
    def test_linkedin_host(self):
        assert jpf.classify_platform("https://www.linkedin.com/jobs/view/123/") == "linkedin"
        assert jpf.classify_platform("https://www.linkedin.com/comm/jobs/view/123/?trackingId=x") == "linkedin"

    def test_indeed_host(self):
        assert jpf.classify_platform("https://il.indeed.com/rc/clk/dl?jk=1") == "indeed"
        assert jpf.classify_platform("https://cts.indeed.com/v3/abc") == "indeed"

    def test_other_host(self):
        assert jpf.classify_platform("https://example.com/careers/123") == "other"

    def test_malformed_url_is_other(self):
        assert jpf.classify_platform("not a url") == "other"


class TestExtractJobLinksRejectsInformationalPages:
    """Real incident: "benefits at Google" and "Google's EEO Policy" -- both URLs
    under /about/careers/applications/... (containing the allowed "career" path
    marker) -- got scored 73-83/100 and WRITTEN to the sheet as job postings."""

    def test_rejects_benefits_and_eeo_pages_by_url_path(self):
        html = """
        <a href="/about/careers/applications/benefits/">benefits at Google</a>
        <a href="/about/careers/applications/eeo/">Google's EEO Policy</a>
        <a href="/job/12345">Senior Software Engineer, Infrastructure</a>
        """
        results = jpf.extract_job_links(html, "https://www.google.com/about/careers/")
        titles = [t.lower() for t, _ in results]
        assert not any("benefit" in t for t in titles)
        assert not any("eeo" in t for t in titles)
        assert any("senior software engineer" in t for t in titles)

    def test_rejects_hiring_faq_by_title_phrase(self):
        html = '<a href="/en-us/careers/ai-hiring-process">View the AI hiring FAQ</a>'
        results = jpf.extract_job_links(html, "https://www.jll.com/en-us/careers/")
        assert results == []

    def test_strips_icon_ligature_text_from_titles(self):
        # Real incident: Material Icons font ligatures leak as literal text --
        # "View the AI hiring FAQ arrow_forward" -- when extracted via a plain regex.
        html = '<a href="/job/999">Senior Data Engineer arrow_forward</a>'
        results = jpf.extract_job_links(html, "https://example.com/careers/")
        assert results and "arrow_forward" not in results[0][0].lower()
        assert results[0][0].strip() == "Senior Data Engineer"


class TestNormalizeJobUrl:
    def test_strips_query_string(self):
        assert jpf.normalize_job_url("https://x.com/job/1?src=abc&utm=123") == "x.com/job/1"

    def test_identical_path_different_query_normalizes_the_same(self):
        a = jpf.normalize_job_url("https://x.com/jobs/results/999-role?src=card1")
        b = jpf.normalize_job_url("https://x.com/jobs/results/999-role?src=card2&session=xyz")
        assert a == b


class TestExtractJobLinksDedupsAcrossTrackingParams:
    def test_same_posting_different_query_strings_counts_once(self):
        # Real incident: Google's careers site linked the SAME dead posting 16 times
        # with varying tracking query strings on one page -- exact-URL dedup let all
        # 16 through as "distinct" candidates, each fetched and 404ing separately.
        html = """
        <a href="/jobs/results/999-role?src=card1">Technical Program Manager</a>
        <a href="/jobs/results/999-role?src=card2&session=abc">Technical Program Manager</a>
        <a href="/jobs/results/999-role?utm_source=email">Technical Program Manager</a>
        """
        results = jpf.extract_job_links(html, "https://www.google.com/about/careers/")
        assert len(results) == 1
