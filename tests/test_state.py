"""Tests for state.py's token-aging warning and CSV-injection guard -- no network calls."""
import os
import time

from mail_agent import config, notifier, state


class TestWarnIfTokensAging:
    def _touch(self, path: str, age_days: float) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))

    def test_no_warning_when_tokens_are_fresh(self, tmp_path, monkeypatch):
        gmail = tmp_path / "token_gmail.json"
        sheets = tmp_path / "token_sheets.json"
        self._touch(str(gmail), 1)
        self._touch(str(sheets), 1)
        monkeypatch.setattr(config, "TOKEN_GMAIL_PATH", str(gmail))
        monkeypatch.setattr(config, "TOKEN_SHEETS_PATH", str(sheets))
        fired = []
        monkeypatch.setattr(notifier, "notify_needs_review", lambda msg: fired.append(msg))
        state.warn_if_tokens_aging()
        assert fired == []

    def test_warns_when_a_token_is_aging(self, tmp_path, monkeypatch):
        # Real motivation: Google's Testing-mode refresh tokens expire after ~7 days
        # regardless of test-user status -- this should fire before that hits, not
        # after a scheduled run crashes with RefreshError.
        gmail = tmp_path / "token_gmail.json"
        sheets = tmp_path / "token_sheets.json"
        self._touch(str(gmail), 6)
        self._touch(str(sheets), 1)
        monkeypatch.setattr(config, "TOKEN_GMAIL_PATH", str(gmail))
        monkeypatch.setattr(config, "TOKEN_SHEETS_PATH", str(sheets))
        fired = []
        monkeypatch.setattr(notifier, "notify_needs_review", lambda msg: fired.append(msg))
        state.warn_if_tokens_aging()
        assert len(fired) == 1 and "Gmail" in fired[0] and "Sheets" not in fired[0]

    def test_missing_token_file_is_not_treated_as_aging(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TOKEN_GMAIL_PATH", str(tmp_path / "missing.json"))
        monkeypatch.setattr(config, "TOKEN_SHEETS_PATH", str(tmp_path / "also_missing.json"))
        fired = []
        monkeypatch.setattr(notifier, "notify_needs_review", lambda msg: fired.append(msg))
        state.warn_if_tokens_aging()
        assert fired == []


class TestCsvSafe:
    def test_leaves_normal_text_untouched(self):
        assert state._csv_safe("Acme Corp") == "Acme Corp"

    def test_neutralizes_formula_leading_characters(self):
        # Real risk: company/title in these CSVs come straight from email content an
        # attacker fully controls, and the files are meant to be opened in Excel/Sheets.
        for payload in ("=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1:A9)", "\tsneaky"):
            safe = state._csv_safe(payload)
            assert safe.startswith("'")
            assert safe[1:] == payload

    def test_handles_none(self):
        assert state._csv_safe(None) == ""


class TestPendingManualLinks:
    def test_round_trip_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PENDING_MANUAL_LINKS_PATH", str(tmp_path / "pending.csv"))
        assert state.load_pending_manual_links() == {}

    def test_upsert_new_entry_defaults_to_given_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PENDING_MANUAL_LINKS_PATH", str(tmp_path / "pending.csv"))
        state.upsert_pending_manual_link("abc123", "Acme", "Engineer", "https://x.com/1", "unprocessed")
        rows = state.load_pending_manual_links()
        assert rows["abc123"]["status"] == "unprocessed"
        assert rows["abc123"]["company"] == "Acme"
        assert rows["abc123"]["date_resolved"] == ""

    def test_upsert_existing_entry_updates_status_and_sets_resolved_date(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PENDING_MANUAL_LINKS_PATH", str(tmp_path / "pending.csv"))
        state.upsert_pending_manual_link("abc123", "Acme", "Engineer", "https://x.com/1", "unprocessed")
        state.upsert_pending_manual_link("abc123", "Acme", "Engineer", "https://x.com/1", "resolved")
        rows = state.load_pending_manual_links()
        assert rows["abc123"]["status"] == "resolved"
        assert rows["abc123"]["date_resolved"] != ""

    def test_csv_injection_in_company_name_is_neutralized(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PENDING_MANUAL_LINKS_PATH", str(tmp_path / "pending.csv"))
        state.upsert_pending_manual_link("abc123", "=cmd|'/c calc'!A1", "Engineer", "https://x.com/1", "unprocessed")
        rows = state.load_pending_manual_links()
        assert rows["abc123"]["company"].startswith("'")

    def test_two_distinct_job_ids_both_persist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PENDING_MANUAL_LINKS_PATH", str(tmp_path / "pending.csv"))
        state.upsert_pending_manual_link("a", "Acme", "Engineer A", "https://x.com/1", "unprocessed")
        state.upsert_pending_manual_link("b", "Beta", "Engineer B", "https://x.com/2", "unprocessed")
        rows = state.load_pending_manual_links()
        assert set(rows.keys()) == {"a", "b"}
