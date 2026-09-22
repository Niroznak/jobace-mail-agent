@echo off
REM Backfills missing url/description on tracked rows via career-site search.
REM Best-effort (DuckDuckGo discovery) -- spot-check output before trusting it.
cd /d "%~dp0"

python backfill_career_links.py %*

if errorlevel 1 (
    echo.
    echo [backfill_career_links] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [backfill_career_links] Done.
)
