@echo off
REM ============================================================
REM  gt-wb-gateway resilient launcher
REM
REM  Keeps the gateway alive. Before (re)starting, it probes
REM  /health on 127.0.0.1:8787; if another instance is already
REM  serving, this launcher exits immediately. That guard makes
REM  the script idempotent, so duplicate launches (task watchdog
REM  + manual start) can never hot-loop against a taken port.
REM ============================================================
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [error] virtualenv not found. Run start.bat once first.
  exit /b 1
)

:loop
"%PY%" -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=2).status==200 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo [watchdog] gateway already healthy on 127.0.0.1:8787 - launcher exits.
  exit /b 0
)

"%PY%" -m gtwb
echo [watchdog] gateway exited (code %errorlevel%), restarting in 10s...
ping -n 11 127.0.0.1 >nul
goto loop
