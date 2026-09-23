"""Tests for pipeline.extract (stage 2) -- structural extraction only, no
fetching/scoring/writing. Mocks classifier.classify_email (the only LLM call this
stage makes) and passes in-memory sheet_rows directly; no network calls.
"""
from mail_agent import classifier
from mail_agent.gmail_client import EmailMessage
from mail_agent.pipeline import extract


def _msg(subject="", sender_name="", sender_email="", body="", snippet="", date_utc="2026-01-01") -> EmailMessage:
    return EmailMessage(
        id="mail-1", thread_id="t1", sender_name=sender_name, sender_email=sender_email,
        subject=subject, body=body, snippet=snippet, date_utc=date_utc,
    )


class TestConnectionRequest:
    def test_returns_no_candidates(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: True)
        result = extract.extract_candidates(_msg(subject="X wants to connect"), [])
        assert result == []


class TestLinkedinDigest:
    DIGEST_BODY = (
        "3 new jobs match your preferences.\n\n"
        "AI Engineer\nAcme Corp\nTel Aviv, Israel\n"
        "View job: https://www.linkedin.com/comm/jobs/view/1111111111/?trackingId=aaa\n"
    )

    def test_new_posting_becomes_opportunity_candidate(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        result = extract.extract_candidates(_msg(subject="3 new jobs", body=self.DIGEST_BODY), [])
        assert len(result) == 1
        c = result[0]
        assert c.kind == "opportunity"
        assert c.source == "linkedin_digest"
        assert c.company == "Acme Corp"
        assert c.title == "AI Engineer"

    def test_already_tracked_and_subject_names_company_becomes_reply_candidate(self, monkeypatch):
        # Real bug this guards against: LinkedIn's own "application sent" email
        # shares this exact digest body shape -- must be routed as a status
        # update, not treated as a brand new opportunity that gets silently
        # deduped away with no status change ever applied.
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(
            classifier, "classify_email",
            lambda msg: {"category": "application_reply", "status": "applied", "company": "Acme Corp",
                         "notes": "", "contact_name": ""},
        )
        tracked_row = {"_row": 5, "company": "Acme Corp", "title": "AI Engineer", "job_id": "whatever"}
        # find_row_by_job_id matches on job_id, so patch it to simulate "already tracked"
        import mail_agent.sheets_client as sc
        monkeypatch.setattr(sc, "find_row_by_job_id", lambda rows, jid: tracked_row)

        result = extract.extract_candidates(
            _msg(subject="Your application was sent to Acme Corp", body=self.DIGEST_BODY), [tracked_row],
        )
        assert len(result) == 1
        c = result[0]
        assert c.kind == "reply"
        assert c.status_signal == "applied"
        assert c.allow_create_if_unmatched is False

    def test_already_tracked_but_not_application_reply_yields_no_candidate(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(classifier, "classify_email", lambda msg: {"category": "other", "status": ""})
        tracked_row = {"_row": 5, "company": "Acme Corp", "title": "AI Engineer", "job_id": "whatever"}
        import mail_agent.sheets_client as sc
        monkeypatch.setattr(sc, "find_row_by_job_id", lambda rows, jid: tracked_row)

        result = extract.extract_candidates(
            _msg(subject="Your application was sent to Acme Corp", body=self.DIGEST_BODY), [tracked_row],
        )
        assert result == []


class TestSingleEmail:
    def test_application_reply_becomes_reply_candidate_allowing_creation(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(classifier, "parse_linkedin_digest", lambda body: [])
        monkeypatch.setattr(
            classifier, "classify_email",
            lambda msg: {"category": "application_reply", "company": "Acme", "role_title": "Engineer",
                         "status": "interview", "notes": "", "contact_name": "", "position_id": ""},
        )
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)

        result = extract.extract_candidates(_msg(subject="Interview invite"), [])
        assert len(result) == 1
        c = result[0]
        assert c.kind == "reply"
        assert c.source == "single_email"
        assert c.allow_create_if_unmatched is True
        assert c.status_signal == "interview"

    def test_blank_role_title_stays_blank_not_subject_defaulted(self, monkeypatch):
        # Real distinction: a blank title here must stay blank for stage 5's
        # matching logic (blank = "safe single-row fallback"), NOT get the subject
        # fallback -- that only applies when stage 5 actually creates a new row.
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(classifier, "parse_linkedin_digest", lambda body: [])
        monkeypatch.setattr(
            classifier, "classify_email",
            lambda msg: {"category": "application_reply", "company": "Acme", "role_title": "",
                         "status": "applied", "notes": "", "contact_name": "", "position_id": ""},
        )
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)

        result = extract.extract_candidates(_msg(subject="Thanks for applying"), [])
        assert result[0].title == ""
        assert result[0].subject == "Thanks for applying"

    def test_job_opportunity_falls_back_to_subject_when_no_role_title(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(classifier, "parse_linkedin_digest", lambda body: [])
        monkeypatch.setattr(
            classifier, "classify_email",
            lambda msg: {"category": "job_opportunity", "company": "Acme", "role_title": "",
                         "location": "", "contact_name": "", "position_id": ""},
        )
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)
        monkeypatch.setattr(classifier, "is_platform_domain", lambda e: False)
        monkeypatch.setattr(classifier, "extract_linkedin_job_id", lambda content: "")

        result = extract.extract_candidates(_msg(subject="AI Engineer at Acme", body="body text"), [])
        assert len(result) == 1
        assert result[0].kind == "opportunity"
        assert result[0].title == "AI Engineer at Acme"

    def test_job_opportunity_with_no_identifiable_company_yields_no_candidate(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(classifier, "parse_linkedin_digest", lambda body: [])
        monkeypatch.setattr(
            classifier, "classify_email",
            lambda msg: {"category": "job_opportunity", "company": "", "role_title": "Engineer"},
        )
        monkeypatch.setattr(classifier, "is_platform_company_name", lambda c: False)
        monkeypatch.setattr(classifier, "is_platform_domain", lambda e: True)
        monkeypatch.setattr(classifier, "parse_generic_digest", lambda subject, body: [])

        result = extract.extract_candidates(_msg(subject="Jobs digest", sender_email="alerts@linkedin.com"), [])
        assert result == []

    def test_not_job_opportunity_recovers_via_generic_digest(self, monkeypatch):
        monkeypatch.setattr(classifier, "is_connection_request", lambda msg: False)
        monkeypatch.setattr(classifier, "parse_linkedin_digest", lambda body: [])
        monkeypatch.setattr(classifier, "classify_email", lambda msg: {"category": "other", "company": ""})
        monkeypatch.setattr(classifier, "is_platform_domain", lambda e: True)
        monkeypatch.setattr(
            classifier, "parse_generic_digest",
            lambda subject, body: [
                {"company": "Acme", "title": "Engineer", "location": "", "url": "", "snippet": "real content"},
                {"company": "Beta", "title": "Analyst", "location": "", "url": "", "snippet": "real content"},
            ],
        )

        result = extract.extract_candidates(_msg(subject="N new jobs", sender_email="alerts@indeed.com"), [])
        assert len(result) == 2
        assert all(c.source == "generic_digest" for c in result)
