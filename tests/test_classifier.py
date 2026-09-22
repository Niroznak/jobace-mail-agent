"""Tests for the deterministic (non-LLM) parts of classifier.py -- no Ollama calls."""
import classifier


class TestStripWorkmodeSuffix:
    def test_strips_remote_city_suffix(self):
        # Real bug: LinkedIn baked this into the digest title itself, breaking
        # title-based dedup/matching against a clean ATS confirmation title.
        result = classifier.strip_workmode_suffix("AI Engineer - Technical Enablement - remote/Tel-Aviv")
        assert result == "AI Engineer - Technical Enablement"

    def test_strips_bare_remote(self):
        result = classifier.strip_workmode_suffix("AI Engineer - Technical Enablement - Remote")
        assert result == "AI Engineer - Technical Enablement"

    def test_strips_hybrid_city(self):
        result = classifier.strip_workmode_suffix("Backend Engineer - Hybrid/Haifa")
        assert result == "Backend Engineer"

    def test_does_not_touch_real_title_suffix(self):
        # Must never strip a genuine subtitle just because it follows a dash.
        result = classifier.strip_workmode_suffix("Senior Data Scientist - Individual Contributor")
        assert result == "Senior Data Scientist - Individual Contributor"

    def test_no_suffix_unchanged(self):
        assert classifier.strip_workmode_suffix("AI Engineer") == "AI Engineer"


class TestIsJuniorOrInternTitle:
    def test_flags_intern(self):
        assert classifier.is_junior_or_intern_title("Data Science Intern")

    def test_flags_junior(self):
        assert classifier.is_junior_or_intern_title("Junior Software Engineer")

    def test_flags_entry_level_variants(self):
        assert classifier.is_junior_or_intern_title("Entry-Level Analyst")
        assert classifier.is_junior_or_intern_title("Entry Level Analyst")

    def test_does_not_flag_senior_roles(self):
        assert not classifier.is_junior_or_intern_title("Senior Backend Engineer")

    def test_does_not_false_positive_on_substring(self):
        # "jr" as a whole word only -- must not match inside unrelated words.
        assert not classifier.is_junior_or_intern_title("Major Account Manager")


class TestIsLocationExcluded:
    def test_allows_north_district(self):
        assert not classifier.is_location_excluded("Haifa")

    def test_allows_tel_aviv_and_herzliya(self):
        assert not classifier.is_location_excluded("Tel Aviv")
        assert not classifier.is_location_excluded("Herzliya")

    def test_allows_remote_and_unknown(self):
        assert not classifier.is_location_excluded("Remote")
        assert not classifier.is_location_excluded("Hybrid")
        assert not classifier.is_location_excluded("")
        assert not classifier.is_location_excluded("Israel")  # vague/unspecific -- never filtered

    def test_excludes_known_out_of_range_cities(self):
        for city in ("Raanana", "Hod Hasharon", "Jerusalem", "Kfar Saba", "Netanya"):
            assert classifier.is_location_excluded(city), city


class TestExtractRequisitionId:
    def test_extracts_job_id_pattern(self):
        text = "Please reference Job ID: R4027488 when applying."
        assert classifier.extract_requisition_id(text) == "R4027488"

    def test_no_id_returns_empty(self):
        assert classifier.extract_requisition_id("No identifiers here.") == ""


class TestExtractLinkedinJobId:
    def test_extracts_from_view_url(self):
        url = "https://www.linkedin.com/comm/jobs/view/4455921120/?trackingId=abc"
        assert classifier.extract_linkedin_job_id(url) == "4455921120"

    def test_no_match_returns_empty(self):
        assert classifier.extract_linkedin_job_id("no url here") == ""


class TestIsPlatformCompanyName:
    def test_flags_known_platforms(self):
        assert classifier.is_platform_company_name("LinkedIn")
        assert classifier.is_platform_company_name("linkedin job alerts")

    def test_does_not_flag_real_employer(self):
        assert not classifier.is_platform_company_name("Micron Technology")


class TestParseLinkedinDigest:
    DIGEST_BODY = """3 new jobs match your preferences.

AI Engineer
Acme Corp
Tel Aviv, Israel
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/1111111111/?trackingId=aaa

-----

Backend Developer
Other Co
Haifa, Israel
View job: https://www.linkedin.com/comm/jobs/view/2222222222/?trackingId=bbb
"""

    def test_parses_multiple_postings(self):
        postings = classifier.parse_linkedin_digest(self.DIGEST_BODY)
        assert len(postings) == 2
        assert postings[0]["title"] == "AI Engineer"
        assert postings[0]["company"] == "Acme Corp"
        assert postings[0]["position_id"] == "1111111111"
        assert postings[1]["title"] == "Backend Developer"
        assert postings[1]["company"] == "Other Co"

    def test_no_postings_in_plain_email(self):
        assert classifier.parse_linkedin_digest("Thank you for your application.") == []

    def test_application_confirmation_shape_still_parses_as_one_posting(self):
        # Real bug: this exact body shape (no "N new jobs" preamble) is what LinkedIn
        # sends for "your application was sent" confirmations -- it must still parse
        # correctly so main.py's job_id-already-tracked routing can detect it.
        body = (
            "Your application was sent to Maytronics\n\n"
            "Algorithm Engineer\nMaytronics\nYizra'el\n"
            "View job: https://www.linkedin.com/comm/jobs/view/4454742342/?trackingId=x"
        )
        postings = classifier.parse_linkedin_digest(body)
        assert len(postings) == 1
        assert postings[0]["company"] == "Maytronics"
        assert postings[0]["position_id"] == "4454742342"
