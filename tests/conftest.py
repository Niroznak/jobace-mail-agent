import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))


@pytest.fixture(autouse=True)
def _no_real_career_page_discovery(monkeypatch):
    """Real bug this caught: verify_position() unconditionally calls
    _ensure_company_tracked(), which reads the real tracked_companies.csv and can fire
    a real network search (SerpAPI/DuckDuckGo) for any company name a test happens to
    use -- every pre-existing verify.py test that didn't explicitly mock
    company_directory suddenly started doing real I/O, and the file's test run time
    went from ~1s to 47s. Disabled by default for every test; a test that specifically
    exercises this feature re-enables it itself via its own monkeypatch call, which
    takes precedence since it runs after this fixture within the same test."""
    from mail_agent import config
    monkeypatch.setattr(config, "AUTO_DISCOVER_CAREER_PAGES", False)
