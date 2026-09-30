@echo off
REM Runs the frequent mail-scan cycle ONLY: fetch new mail, verify/score/reconcile.
REM
REM The liveness/completeness/duplicate integrity check (formerly
REM review_closed_positions.py + validate_sheet.py, run here on every cycle) moved to
REM check_open_positions.py, run once a day from run_morning.bat instead. Real
REM incident: running that liveness re-fetch (one network call per open row) on every
REM ~10-minute cycle is what tripped LinkedIn's bot-detection (HTTP 429/999) 22 times
REM in a single run. Once a day cuts that request volume by roughly two orders of
REM magnitude.
REM
REM backfill_career_links.py is DISABLED (commented out below) as of 2026-09-21:
REM its career-site fuzzy title-match produced a confirmed false positive (a
REM "found" position that did not actually exist on the company's career page).
REM config.ENABLE_CAREER_SITE_SEARCH = False also short-circuits the same code
REM path inside position_resolver.py, so this is belt-and-suspenders -- re-enable
REM both together once the matching logic is tightened and verified.
cd /d "%~dp0"

python scripts\main.py %*
REM python scripts\backfill_career_links.py

if errorlevel 1 (
    echo.
    echo [run_mail_agent] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [run_mail_agent] Done.
)
