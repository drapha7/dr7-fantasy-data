@echo off
REM dR7 Fantasy - daily NBA data pull. Called by Windows Task Scheduler.
cd /d "%~dp0"
python pull.py >> pull-task.log 2>&1
exit /b %errorlevel%
