"""Tests for oauth_helpers.get_credentials' self-healing reauth path -- no real network
or browser calls; the interactive flow and refresh call are mocked."""
import concurrent.futures

import pytest
from google.auth.exceptions import RefreshError

from mail_agent import notifier, oauth_helpers


class _FakeCreds:
    def __init__(self, valid=False, expired=False, refresh_token=None, refresh_raises=False):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._refresh_raises = refresh_raises

    def refresh(self, request):
        if self._refresh_raises:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        self.valid = True

    def to_json(self):
        return "{}"


class TestGetCredentials:
    def test_valid_existing_token_is_used_as_is(self, tmp_path, monkeypatch):
        token = tmp_path / "token.json"
        token.write_text("{}", encoding="utf-8")
        fake = _FakeCreds(valid=True)
        monkeypatch.setattr(oauth_helpers.Credentials, "from_authorized_user_file", lambda *a, **k: fake)
        result = oauth_helpers.get_credentials("Gmail", str(token), ["scope"])
        assert result is fake

    def test_expired_but_refreshable_token_is_refreshed_in_place(self, tmp_path, monkeypatch):
        token = tmp_path / "token.json"
        token.write_text("{}", encoding="utf-8")
        fake = _FakeCreds(valid=False, expired=True, refresh_token="rt")
        monkeypatch.setattr(oauth_helpers.Credentials, "from_authorized_user_file", lambda *a, **k: fake)
        result = oauth_helpers.get_credentials("Gmail", str(token), ["scope"])
        assert result is fake and fake.valid is True

    def test_revoked_refresh_token_self_heals_via_browser_flow(self, tmp_path, monkeypatch):
        # Real incident: this used to just notify and re-raise, silently stopping the
        # whole pipeline. It should now auto-run the same interactive flow reauth.py
        # performs by hand.
        token = tmp_path / "token.json"
        token.write_text("{}", encoding="utf-8")
        stale = _FakeCreds(valid=False, expired=True, refresh_token="rt", refresh_raises=True)
        fresh = _FakeCreds(valid=True)
        monkeypatch.setattr(oauth_helpers.Credentials, "from_authorized_user_file", lambda *a, **k: stale)
        monkeypatch.setattr(oauth_helpers, "_run_interactive_flow", lambda path, scopes: fresh)
        notified = []
        monkeypatch.setattr(notifier, "notify_needs_review", lambda msg: notified.append(msg))
        result = oauth_helpers.get_credentials("Gmail", str(token), ["scope"])
        assert result is fresh
        assert any("expired" in m for m in notified)

    def test_no_existing_token_runs_interactive_flow(self, tmp_path, monkeypatch):
        token = tmp_path / "does_not_exist.json"
        fresh = _FakeCreds(valid=True)
        monkeypatch.setattr(oauth_helpers, "_run_interactive_flow", lambda path, scopes: fresh)
        result = oauth_helpers.get_credentials("Sheets", str(token), ["scope"])
        assert result is fresh
        assert token.read_text(encoding="utf-8") == "{}"

    def test_reauth_timeout_notifies_and_raises_instead_of_hanging(self, tmp_path, monkeypatch):
        # The safety net for an unattended run: no click ever comes, so this must give
        # up and raise rather than block the process forever.
        token = tmp_path / "token.json"
        monkeypatch.setattr(oauth_helpers, "_REAUTH_TIMEOUT_SECONDS", 0.05)

        def _never_returns(path, scopes):
            import time
            time.sleep(5)

        monkeypatch.setattr(oauth_helpers, "_run_interactive_flow", _never_returns)
        notified = []
        monkeypatch.setattr(notifier, "notify_needs_review", lambda msg: notified.append(msg))
        with pytest.raises(RefreshError):
            oauth_helpers.get_credentials("Gmail", str(token), ["scope"])
        assert any("timed out" in m for m in notified)
