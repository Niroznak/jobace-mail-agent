"""Tests for the deterministic (non-LLM) parts of classifier.py -- no Ollama calls."""
from mail_agent import classifier
from mail_agent import llm_client
from mail_agent.gmail_client import EmailMessage


def _msg(subject: str, sender_name: str = "", sender_email: str = "", body: str = "") -> EmailMessage:
    return EmailMessage(
        id="x", thread_id="x", sender_name=sender_name, sender_email=sender_email,
        subject=subject, body=body, snippet=body, date_utc="2026-01-01",
    )


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


class TestApplicationAckSubjectOverride:
    """Real incident: qwen2.5:7b reproducibly (3/3 attempts) misclassified "Thank you
    for your application to Warner Music Group" -- a templated ATS acknowledgment with
    zero real ambiguity -- as category=job_opportunity with an empty company. Combined
    with the sender being on a known ATS domain, that triggered main.py's "no
    identifiable hiring company" skip path, which marks the message read -- permanently
    dropping a real status update with no retry. classify_email now overrides a wrong
    category when the subject unmistakably matches this pattern."""

    def test_overrides_wrong_job_opportunity_category(self, monkeypatch):
        monkeypatch.setattr(
            llm_client, "call_json",
            lambda prompt: {"category": "job_opportunity", "company": "", "role_title": "",
                             "position_id": "", "contact_name": "", "location": "", "status": "", "notes": ""},
        )
        msg = _msg("Thank you for your application to Warner Music Group", sender_name="Warner Music Group")
        result = classifier.classify_email(msg)
        assert result["category"] == "application_reply"
        assert result["company"] == "Warner Music Group"
        assert result["status"] == "applied"

    def test_does_not_override_a_correct_category_or_populated_fields(self, monkeypatch):
        # Should never clobber real LLM-extracted fields when they're already present.
        monkeypatch.setattr(
            llm_client, "call_json",
            lambda prompt: {"category": "application_reply", "company": "Acme", "role_title": "Engineer",
                             "position_id": "", "contact_name": "", "location": "", "status": "interview", "notes": ""},
        )
        msg = _msg("Thank you for your application to Acme", sender_name="Acme")
        result = classifier.classify_email(msg)
        assert result["status"] == "interview"

    def test_unrelated_subject_is_not_affected(self, monkeypatch):
        monkeypatch.setattr(
            llm_client, "call_json",
            lambda prompt: {"category": "job_opportunity", "company": "Acme", "role_title": "Engineer",
                             "position_id": "", "contact_name": "", "location": "", "status": "", "notes": ""},
        )
        msg = _msg("New job alert: Engineer at Acme", sender_name="LinkedIn")
        result = classifier.classify_email(msg)
        assert result["category"] == "job_opportunity"


class TestLinkedinApplicationSentSubject:
    """Real incident: "Nir, your application was sent to BigBear.ai" was classified
    job_opportunity/empty company by the 7b model, and the subject safety net didn't
    cover LinkedIn's own "was sent" phrasing -- the confirmation was silently skipped
    and the row's status never moved to applied."""

    def test_overrides_for_linkedin_was_sent_phrasing(self, monkeypatch):
        monkeypatch.setattr(
            llm_client, "call_json",
            lambda prompt, **k: {"category": "job_opportunity", "company": "", "role_title": "",
                                  "position_id": "", "contact_name": "", "location": "", "status": "", "notes": ""},
        )
        msg = _msg("Nir, your application was sent to BigBear.ai", sender_name="LinkedIn")
        result = classifier.classify_email(msg)
        assert result["category"] == "application_reply"
        assert result["status"] == "applied"


class TestConfirmCompanyNameRejectsSentences:
    def test_sentence_shaped_answer_keeps_original_guess(self, monkeypatch):
        monkeypatch.setattr(
            llm_client, "call_json",
            lambda prompt, **k: {"company": "An innovative startup developing AI-driven predictive platforms for health monitoring."},
        )
        assert classifier.confirm_company_name("page text", "Megayeset") == "Megayeset"

    def test_short_real_name_is_accepted(self, monkeypatch):
        monkeypatch.setattr(llm_client, "call_json", lambda prompt, **k: {"company": "BigBear.ai"})
        assert classifier.confirm_company_name("page text", "CargoSeer") == "BigBear.ai"


class TestParseIndeedDigest:
    BODY = (
        "Indeed Job Alert\n20 new ai engineer jobs in Haifa\n\n"
        "AI Regulatory Engineer\nGE HEALTHCARE - Haifa, Israel\nan engineer to ensure... more...\n6 days ago\n"
        "https://il.indeed.com/rc/clk/dl?jk=aaa&from=ja\n\n"
        "Data\xa0&\xa0AI Platform Team Leader\nGE HEALTHCARE - Haifa, Israel\nthat accelerate AI deployment...\n6 days ago\n"
        "https://il.indeed.com/rc/clk/dl?jk=bbb&from=ja\n"
    )

    def test_parses_every_block(self):
        posts = classifier.parse_indeed_digest(self.BODY)
        assert [p["title"] for p in posts] == ["AI Regulatory Engineer", "Data & AI Platform Team Leader"]
        assert posts[0]["company"] == "GE HEALTHCARE" and posts[0]["location"] == "Haifa, Israel"
        assert posts[1]["url"].endswith("jk=bbb&from=ja")

    def test_non_indeed_body_returns_empty(self):
        assert classifier.parse_indeed_digest("Thank you for your application.") == []
