@echo off
rem Refresh the dashboard from this machine. Called by Task Scheduler.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
python scripts\run_refresh_all.py --attempts 3 --retry-delay 60 --env-file "%~dp0refresh.env"
set EXITCODE=%ERRORLEVEL%
echo %DATE% %TIME% exit=%EXITCODE% >> logs\scheduler.log
exit /b %EXITCODE%
