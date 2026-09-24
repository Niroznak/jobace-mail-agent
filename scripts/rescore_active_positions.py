"""One-off re-score of every 'not applied yet' row under the current code-enforced
scoring rules (required-skill blockers, computed score), filling the `requirements`
column with the extracted skills + CV coverage.

Only the fit_score, requirements and notes columns are written -- never status. Rows
that now fall below FIT_SCORE_THRESHOLD are reported, not demoted: that call is yours.
Rows whose stored description is too short to judge (an Indeed teaser) are skipped.

Usage:
    python rescore_active_positions.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mail_agent import config, cv_matcher, job_page_fetcher, sheets_client
from mail_agent.pipeline.verify import MIN_DIGEST_DESCRIPTION_CHARS

logging.basicConfig(level=logging.WARNING, format="%(message)s")
_ASCII = lambda s: str(s).encode("ascii", "replace").decode()


def main(dry_run: bool) -> None:
    sheets = sheets_client.get_sheets_service()
    rows = sheets_client.fetch_all_rows(sheets)
    targets = [
        r for r in sheets_client.active_rows(rows)
        if (r.get("status") or "").strip().lower() in ("", config.STATUS_NOT_APPLIED_YET)
        and (r.get("status") or "").strip().lower() != "nr"
    ]
    print(f"{len(targets)} not-applied active row(s) to re-score")
    below, skipped = [], []
    for r in targets:
        n, title, company = r["_row"], r.get("title", ""), r.get("company", "")
        desc = r.get("description") or ""
        if len(desc) >= config.DESCRIPTION_STORE_CHARS - 100:  # stored text was cut -- re-read the full posting
            full = job_page_fetcher.refetch_full_description(r.get("url", ""))
            if len(full) > len(desc):
                desc = full
        if len(desc) < MIN_DIGEST_DESCRIPTION_CHARS:
            skipped.append((n, title, company, len(desc)))
            continue
        try:
            res = cv_matcher.score_job_email(company, title, desc)
        except Exception as exc:
            print(f"  row {n}: scoring failed ({exc.__class__.__name__}), left as-is")
            continue
        new = res.get("score", 0)
        checked = res.get("requirements_checked") or []
        print(f"  row {n}: {_ASCII(title)[:45]} @ {_ASCII(company)[:20]}  {r.get('fit_score')} -> {new}"
              f"{'  BELOW THRESHOLD' if new < config.FIT_SCORE_THRESHOLD else ''}")
        if new < config.FIT_SCORE_THRESHOLD:
            below.append((n, title, company, new))
        if not dry_run:
            fields = {"fit_score": new, "description": job_page_fetcher.focus_description(desc, config.DESCRIPTION_STORE_CHARS)}
            if checked:
                fields["requirements"] = json.dumps(checked, ensure_ascii=False, separators=(",", ":"))
            sheets_client.update_row_fields(sheets, n, fields)
    print(f"\nBelow threshold ({len(below)}):")
    for n, t, c, s in below:
        print(f"  row {n}: {_ASCII(t)} @ {_ASCII(c)} score={s}")
    print(f"Skipped, description too short to judge ({len(skipped)}):")
    for n, t, c, ln in skipped:
        print(f"  row {n}: {_ASCII(t)} @ {_ASCII(c)} ({ln} chars)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
