@echo off
REM Morning slot only: runs the full pipeline once per day even if the wake/logon
REM trigger fires more than once (e.g. Task Scheduler's missed-start catch-up landing
REM close to the normal 8am trigger, or multiple unlocks before 8am has been "used").
cd /d "%~dp0"

python scripts\morning_flag.py check
if %errorlevel%==0 (
    echo [run_morning] Already ran today, skipping.
    exit /b 0
)

call run_mail_agent.bat
if errorlevel 1 (
    echo [run_morning] run_mail_agent.bat FAILED, not marking morning flag.
    exit /b 1
)

REM Proactive career-page scan, morning-only (career pages don't change fast enough
REM to justify running this at noon/5pm too) -- but only once the mail queue actually
REM finished draining. main.py leaves data\mail_queue_incomplete.flag behind if it
REM stopped early (3 consecutive Ollama failures, or a large backlog not fully
REM attempted this run) -- running the career-page scan on top of that would compete
REM with a still-backed-up mail queue for the same slow local LLM.
if exist "data\mail_queue_incomplete.flag" (
    echo [run_morning] Mail queue not fully drained yet ^(see data\mail_queue_incomplete.flag^) -- skipping career-page scan this run.
) else (
    python scripts\scan_career_pages.py
)

python scripts\morning_flag.py mark
echo [run_morning] Done, flag set for today.
