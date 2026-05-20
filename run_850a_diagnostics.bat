@echo off
setlocal

REM Run Danfoss 850A API diagnostics on the customer field PC.
REM Usage:
REM   run_850a_diagnostics.bat
REM   run_850a_diagnostics.bat config.customer.json
REM   run_850a_diagnostics.bat config.customer.json http://172.28.238.109/html/xml.cgi

set CONFIG=%~1
if "%CONFIG%"=="" set CONFIG=config.customer.example.json

set ENDPOINT_ARG=
if not "%~2"=="" set ENDPOINT_ARG=--endpoint "%~2"

python --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  echo Install Python or run the packaged EXE workflow instead.
  exit /b 1
)

echo [INFO] Running 850A diagnostics with config: %CONFIG%
python diagnose_850a.py --config "%CONFIG%" %ENDPOINT_ARG%

endlocal
