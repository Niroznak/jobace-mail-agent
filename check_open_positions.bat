@echo off
REM Manual/on-demand run of the daily integrity check (liveness, completeness,
REM duplicates). Runs automatically once a day from run_morning.bat -- use this to
REM run it again by hand, e.g. to see today's report without waiting for tomorrow.
cd /d "%~dp0"

python scripts\check_open_positions.py %*

if errorlevel 1 (
    echo.
    echo [check_open_positions] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [check_open_positions] Done.
)
