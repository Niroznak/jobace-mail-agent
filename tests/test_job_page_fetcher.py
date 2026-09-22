"""Tests for the pure text-processing helpers in job_page_fetcher.py -- no network."""
import job_page_fetcher as jpf


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
