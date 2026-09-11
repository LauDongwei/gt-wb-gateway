@echo off
REM gt-wb-gateway launcher (ASCII only to avoid Windows codepage issues)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtualenv...
  uv venv .venv
  if errorlevel 1 goto :novenv
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  if errorlevel 1 goto :nodeps
)

echo [run] starting gt-wb-gateway on http://127.0.0.1:8787
.venv\Scripts\python.exe -m gtwb %*
goto :eof

:novenv
echo [error] failed to create virtualenv. Is uv installed? https://docs.astral.sh/uv/
exit /b 1

:nodeps
echo [error] failed to install dependencies.
exit /b 1
