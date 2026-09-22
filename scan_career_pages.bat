@echo off
REM Proactively scans tracked companies' career pages for new postings.
REM Best-effort (many career sites are JS-rendered and yield no links) -- see README
REM Known Limitations.
cd /d "%~dp0"

python scan_career_pages.py %*

if errorlevel 1 (
    echo.
    echo [scan_career_pages] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [scan_career_pages] Done.
)
