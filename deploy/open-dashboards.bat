@echo off
chcp 65001 >nul
title gt-wb-gateway Dashboards
setlocal EnableDelayedExpansion

REM ============================================================
REM  One-click launcher for all gt-wb-gateway dashboards.
REM
REM  Flow: probe each service -> if down, start it -> poll until
REM        ready -> open browsers. All probes run with proxy vars
REM        cleared, because this box exports HTTP_PROXY and a
REM        probe to 127.0.0.1 would otherwise detour via proxy
REM        and be misread as "service is dead".
REM
REM  Dashboards:
REM    8735  status board  status_server.py  (KPI / hourly / clients)
REM    8731  usage  board  wb-usage-widget   (token / perf / accounts)
REM    8787  gateway API  (no web page, liveness probe only)
REM ============================================================

set "GW=D:\workbuddy\研究院\gt-wb-gateway"
set "WIDGET=D:\workbuddy\研究院\wb-usage-widget"
set "PY=%GW%\.venv\Scripts\python.exe"
REM prefer the WorkBuddy-managed pythonw, fall back to PATH
set "PYW=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

REM -- proxy hygiene: keep every probe on a clean environment --
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

echo.
echo  === gt-wb-gateway Dashboards ===
echo.

REM ---------- probe all three upfront ----------
call :probe 8787 /health      GWOK
call :probe 8735 /health      SBOK
call :probe 8731 /api/health  UBOK

REM ---------- 1) gateway 8787 ----------
if "!GWOK!"=="1" (
  echo  [OK]    gateway 8787 is up
) else (
  echo  [START] gateway 8787 is down  -^> trigger scheduled task
  powershell -NoProfile -Command "Start-ScheduledTask -TaskName 'gt-wb-gateway'" >nul 2>&1
)

REM ---------- 2) status board 8735 ----------
if "!SBOK!"=="1" (
  echo  [OK]    status board 8735 is up
) else (
  echo  [START] status board 8735 is down  -^> launch sidecar
  start "gw-dashboard" /min "%PYW%" "%GW%\deploy\status_server.py" --port 8735
)

REM ---------- 3) usage board 8731 ----------
if "!UBOK!"=="1" (
  echo  [OK]    usage board 8731 is up
) else (
  echo  [START] usage board 8731 is down  -^> trigger scheduled task
  powershell -NoProfile -Command "Start-ScheduledTask -TaskName 'ldw-usage-widget'" >nul 2>&1
  call :probe 8731 /api/health UBOK
  if "!UBOK!"=="0" start "wb-usage" /min "%PYW%" "%WIDGET%\server.py" 8731
)

REM ---------- wait until ready (max ~15s) ----------
echo.
echo  waiting for services...
set /a tries=0
:wait
set /a tries+=1
call :probe 8787 /health      A
call :probe 8735 /health      B
call :probe 8731 /api/health  C
if "!A!!B!!C!"=="111" goto openall
if !tries! lss 15 (
  powershell -NoProfile -Command "Start-Sleep -Milliseconds 1000" >nul 2>&1
  goto wait
)

:openall
echo.
if not "!A!!B!!C!"=="111" (
  echo  [WARN] some services are not ready, opening what is available:
  if "!A!"=="0" echo          - gateway 8787 not ready ^(board data may not refresh^)
  if "!B!"=="0" echo          - status board 8735 not ready
  if "!C!"=="0" echo          - usage board 8731 not ready
  echo.
)

echo  opening:
if "!B!"=="1" (
  start "" "http://127.0.0.1:8735/"
  echo     - status board  http://127.0.0.1:8735/
)
if "!C!"=="1" (
  start "" "http://127.0.0.1:8731/"
  echo     - usage  board  http://127.0.0.1:8731/
)
echo.
echo  Press any key to close this window...
pause >nul
exit /b 0

REM ============================================================
REM  :probe <port> <path> <outVar>    1 = reachable, 0 = not
REM ============================================================
:probe
"%PY%" -c "import sys,urllib.request as u;sys.exit(0 if u.urlopen('http://127.0.0.1:%~1%~2',timeout=2).status==200 else 1)" >nul 2>&1
if errorlevel 1 (set "%~3=0") else (set "%~3=1")
exit /b 0
