@echo off
REM Reviews tracked positions' links and grays out ones that are no longer open.
cd /d "%~dp0"

python scripts\review_closed_positions.py %*

if errorlevel 1 (
    echo.
    echo [review_closed_positions] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [review_closed_positions] Done.
)
