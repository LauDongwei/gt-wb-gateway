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

REM -- Probe hygiene: interactive shells on this box export HTTP_PROXY/HTTPS_PROXY.
REM -- python urllib honors them, so a probe to 127.0.0.1 would detour through the
REM -- proxy, fail, and the watchdog would "resurrect" a healthy gateway, spawning
REM -- port-conflict zombies. Clear proxy vars before any probe.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [error] virtualenv not found. Run start.bat once first.
  exit /b 1
)

:loop
REM -- sidecar: keep the read-only status dashboard alive on 8735 --
REM (checked BEFORE the gateway early-exit so it revives on every trigger)
"%PY%" -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8735/health',timeout=2).status==200 else 1)" >nul 2>&1
if errorlevel 1 (
  echo [watchdog] dashboard down - starting sidecar on 127.0.0.1:8735
  start "gw-dashboard" /min "%PY%" "%~dp0status_server.py" --port 8735
)

"%PY%" -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=2).status==200 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo [watchdog] gateway already healthy on 127.0.0.1:8787 - launcher exits.
  exit /b 0
)

"%PY%" -m gtwb
echo [watchdog] gateway exited (code %errorlevel%), restarting in 10s...
ping -n 11 127.0.0.1 >nul
goto loop
