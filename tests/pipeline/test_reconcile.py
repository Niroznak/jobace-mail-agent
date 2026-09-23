"""Tests for pipeline.reconcile (stage 5) -- mocks position_sheet.append_position
and sheets_client.update_row_fields (the only real-write calls this stage makes);
no network calls. Every candidate regardless of source funnels through these same
functions, which is the direct fix for 5 near-duplicate write sites each getting
some detail slightly wrong.
"""
from mail_agent import guardrails
from mail_agent import notifier
from mail_agent import position_sheet
from mail_agent import sheets_client
from mail_agent.pipeline import reconcile
from mail_agent.pipeline.types import Candidate, ScoredItem, VerifiedPosition


def _reply(**overrides) -> Candidate:
    base = dict(mail_id="m1", kind="reply", source="single_email", company="Acme", title="Engineer",
                status_signal="applied", allow_create_if_unmatched=True)
    base.update(overrides)
    return Candidate(**base)


def _scored_item(**overrides) -> ScoredItem:
    candidate = Candidate(mail_id="m1", kind="opportunity", source="single_email", company="Acme", title="Engineer",
                           date_utc="2026-01-01")
    verified = VerifiedPosition(candidate=candidate, company="Acme", title="Engineer", url="https://x",
                                 description="real desc", job_id="jid123")
    base = dict(verified=verified, score=80, summary="Great fit.")
    base.update(overrides)
    return ScoredItem(**base)


class TestReconcileNewPosition:
    def test_inserts_and_notifies(self, monkeypatch):
        monkeypatch.setattr(position_sheet, "append_position", lambda sheets, record: 42)
        notified = {}
        monkeypatch.setattr(notifier, "notify_new_match", lambda company, title, score: notified.setdefault("called", True))

        sheet_rows = []
        result = reconcile.reconcile(_scored_item(), sheet_rows, sheets=object(), dry_run=False)

        assert result.action == "inserted"
        assert result.row_number == 42
        assert notified.get("called") is True
        assert sheet_rows[0]["title"] == "Engineer"  # in-memory mirror matches the real write, not a blank stub

    def test_dry_run_never_calls_append_position(self, monkeypatch):
        called = {}
        monkeypatch.setattr(position_sheet, "append_position", lambda sheets, record: called.setdefault("yes", True))
        result = reconcile.reconcile(_scored_item(), [], sheets=object(), dry_run=True)
        assert "yes" not in called
        assert result.row_number == -1


class TestReconcileReplySingleEmail:
    def test_matched_row_gets_status_applied(self, monkeypatch):
        matched = {"_row": 7, "status": "not applied yet", "notes": ""}
        monkeypatch.setattr(guardrails, "resolve_reply_target_row", lambda rows, company, title: (matched, [], "single_company_row"))
        updated = {}
        monkeypatch.setattr(sheets_client, "update_row_fields", lambda sheets, row, fields: updated.setdefault("fields", fields))

        result = reconcile.reconcile(_reply(status_signal="applied"), [matched], sheets=object(), dry_run=False)

        assert result.action == "updated"
        assert updated["fields"]["status"] == "applied"
        assert matched["status"] == "applied"  # in-memory mirror updated too

    def test_ambiguous_match_is_flagged_not_guessed(self, monkeypatch):
        candidates = [{"_row": 1}, {"_row": 2}]
        monkeypatch.setattr(guardrails, "resolve_reply_target_row", lambda rows, company, title: (None, candidates, ""))
        notified = {}
        monkeypatch.setattr(notifier, "notify_needs_review", lambda msg: notified.setdefault("msg", msg))

        result = reconcile.reconcile(_reply(), [], sheets=object(), dry_run=False)

        assert result.action == "ambiguous"
        assert "msg" in notified

    def test_unmatched_and_creation_allowed_creates_new_row(self, monkeypatch):
        monkeypatch.setattr(guardrails, "resolve_reply_target_row", lambda rows, company, title: (None, [], ""))
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: None)
        monkeypatch.setattr(position_sheet, "append_position", lambda sheets, record: 99)

        result = reconcile.reconcile(_reply(allow_create_if_unmatched=True), [], sheets=object(), dry_run=False)

        assert result.action == "inserted"
        assert result.row_number == 99

    def test_unmatched_and_creation_not_allowed_is_dropped(self, monkeypatch):
        # Digest-derived replies never create a new row -- mirrors original
        # behavior exactly (a digest-shaped status update with no resolvable
        # target is skipped, never promoted to a new row).
        monkeypatch.setattr(guardrails, "resolve_reply_target_row", lambda rows, company, title: (None, [], ""))

        result = reconcile.reconcile(_reply(source="linkedin_digest", allow_create_if_unmatched=False), [], sheets=object(), dry_run=False)

        assert result.action == "dropped"


class TestReconcileReplyLinkedinDigest:
    def test_job_id_match_takes_priority_over_title_resolution(self, monkeypatch):
        matched = {"_row": 3, "status": "not applied yet", "notes": ""}
        monkeypatch.setattr(sheets_client, "find_row_by_job_id", lambda rows, jid: matched)
        called_resolve = {}
        monkeypatch.setattr(
            guardrails, "resolve_reply_target_row",
            lambda rows, company, title: called_resolve.setdefault("called", True) or (None, [], ""),
        )
        monkeypatch.setattr(sheets_client, "update_row_fields", lambda sheets, row, fields: None)

        result = reconcile.reconcile(_reply(source="linkedin_digest", status_signal="rejected"), [matched], sheets=object(), dry_run=False)

        assert result.action == "updated"
        assert "called" not in called_resolve  # job_id match short-circuited before ever needing title resolution
