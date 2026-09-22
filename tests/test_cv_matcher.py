"""Tests for cv_matcher's code-enforced hard-requirement score cap -- no Ollama calls."""
from mail_agent import cv_matcher
from mail_agent import llm_client


class TestApplyHardRequirementCap:
    def test_caps_score_when_hard_gap_present(self):
        result = {
            "score": 65,
            "hard_requirement_gaps": ["Strong Java expertise required"],
            "summary": "Good overall fit.",
        }
        capped = cv_matcher._apply_hard_requirement_cap(result)
        assert capped["score"] == 25
        assert "Java" in capped["summary"]
        assert "capped 65->25" in capped["summary"]

    def test_leaves_score_untouched_when_no_hard_gaps(self):
        result = {"score": 85, "hard_requirement_gaps": [], "summary": "Strong match."}
        result_copy = dict(result)
        capped = cv_matcher._apply_hard_requirement_cap(result)
        assert capped["score"] == 85
        assert capped["summary"] == result_copy["summary"]

    def test_missing_hard_requirement_gaps_key_is_safe(self):
        result = {"score": 90, "summary": "Great fit."}
        capped = cv_matcher._apply_hard_requirement_cap(result)
        assert capped["score"] == 90

    def test_does_not_raise_score_if_already_below_cap(self):
        result = {"score": 10, "hard_requirement_gaps": ["Missing X"], "summary": "Weak."}
        capped = cv_matcher._apply_hard_requirement_cap(result)
        assert capped["score"] == 10


class TestScoreJobEmailSanityGate:
    """Pre-scoring gate added after two real incidents: a JS-rendered page's
    bootstrap JSON, and a real-but-irrelevant page (nav chrome + product blurb), both
    got LLM-scored as if they were real job descriptions. Verifies the gate short-
    circuits BEFORE any Ollama call -- monkeypatches call_json to raise if reached."""

    def _forbid_llm_call(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise AssertionError("llm_client.call_json should never be reached for non-posting content")
        monkeypatch.setattr(llm_client, "call_json", _raise)

    def test_skips_scoring_for_content_with_no_job_signal(self, monkeypatch):
        self._forbid_llm_call(monkeypatch)
        result = cv_matcher.score_job_email("Acme", "Engineer", "Home About Contact Products Careers")
        assert result["score"] == 0
        assert result["hard_requirement_gaps"] == []
        assert "doesn't look like a real job posting" in result["summary"]

    def test_skips_scoring_for_json_dump_masquerading_as_content(self, monkeypatch):
        self._forbid_llm_call(monkeypatch)
        # Real case: a Workday page's UI config literally contains the string
        # '"label": "Job Description"' -- passes a naive word-based check, but the
        # surrounding text is dense encoded JSON, not a real posting.
        json_dump = '{&#34;label&#34;: &#34;Job Description&#34;, &#34;id&#34;: &#34;x&#34;}' * 50
        result = cv_matcher.score_job_email("Acme", "Engineer", json_dump)
        assert result["score"] == 0

    def test_does_not_skip_real_posting_content(self, monkeypatch):
        called = {}

        def _fake_call_json(prompt, **kwargs):
            called["yes"] = True
            return {"score": 80, "hard_requirement_gaps": [], "summary": "Good fit."}

        monkeypatch.setattr(llm_client, "call_json", _fake_call_json)
        monkeypatch.setattr(cv_matcher, "ensure_profile", lambda: {"skills": []})
        real_content = (
            "About the role: we are looking for a Senior Engineer.\n"
            "Requirements:\n- 5+ years Python\n- Strong communication skills\n"
            "Responsibilities:\n- Build things\n- Ship things"
        )
        result = cv_matcher.score_job_email("Acme", "Senior Engineer", real_content)
        assert called.get("yes") is True
        assert result["score"] == 80
