"""Windows toast notifications, fired only on new fitting-job matches."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def notify_new_match(company: str, title: str, score: int) -> None:
    _notify(f"New job match added: {company} - {title} ({score}/100)")


def notify_needs_review(message: str) -> None:
    _notify(message)


def _notify(message: str) -> None:
    try:
        from win11toast import notify
        notify("JobAce Mail Agent", message)
    except Exception as exc:
        logger.warning("Toast notification failed (%s); message was: %s", exc, message)
