@echo off
REM Reauthorizes both Gmail and Sheets in one run. Use when a script fails with
REM "Token has been expired or revoked" (Google's Testing-mode ~7-day expiry).
cd /d "%~dp0"

python scripts\reauth.py %*

if errorlevel 1 (
    echo.
    echo [reauth] FAILED with exit code %errorlevel%
    pause
) else (
    echo.
    echo [reauth] Done.
)
