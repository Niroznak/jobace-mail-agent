"""Tests for pipeline.score (stage 4) -- mocks cv_matcher.score_job_email, the
only external call this stage makes."""
from mail_agent import cv_matcher
from mail_agent.pipeline import score
from mail_agent.pipeline.types import Candidate, VerifiedPosition


def _verified(**overrides) -> VerifiedPosition:
    candidate = Candidate(mail_id="m1", kind="opportunity", source="single_email", company="Acme", title="Engineer")
    base = dict(candidate=candidate, company="Acme", title="Engineer", url="https://x", description="real content", job_id="abc123")
    base.update(overrides)
    return VerifiedPosition(**base)


class TestScorePosition:
    def test_above_threshold_returns_scored_item(self, monkeypatch):
        monkeypatch.setattr(cv_matcher, "score_job_email", lambda company, title, content: {"score": 80, "summary": "Great fit."})
        result = score.score_position(_verified())
        assert result is not None
        assert result.score == 80
        assert result.summary == "Great fit."

    def test_below_threshold_returns_none_and_logs_skipped(self, monkeypatch):
        logged = {}
        monkeypatch.setattr(cv_matcher, "score_job_email", lambda company, title, content: {"score": 20, "summary": "Poor fit."})
        monkeypatch.setattr("mail_agent.pipeline.score.state.log_skipped_candidate", lambda *a, **k: logged.setdefault("called", (a, k)))
        result = score.score_position(_verified())
        assert result is None
        assert logged.get("called") is not None
