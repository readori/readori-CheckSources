@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%source_validator_cli.py" %*
) else (
  python "%ROOT%source_validator_cli.py" %*
)
exit /b %errorlevel%

