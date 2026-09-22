@echo off
REM Runs the full pipeline: review/gray-out closed positions first (keeps dedup
REM working against a clean active set), then the mail scan and job processing,
REM then validate the sheet for rows missing company/title or url/description.
REM
REM backfill_career_links.py is DISABLED (commented out below) as of 2026-09-21:
REM its career-site fuzzy title-match produced a confirmed false positive (a
REM "found" position that did not actually exist on the company's career page).
REM config.ENABLE_CAREER_SITE_SEARCH = False also short-circuits the same code
REM path inside position_resolver.py, so this is belt-and-suspenders -- re-enable
REM both together once the matching logic is tightened and verified.
cd /d "%~dp0"

python scripts\review_closed_positions.py
python scripts\main.py %*
REM python scripts\backfill_career_links.py
python scripts\validate_sheet.py

if errorlevel 1 (
    echo.
    echo [run_mail_agent] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [run_mail_agent] Done.
)
