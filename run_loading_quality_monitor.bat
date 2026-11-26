@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

set "LOG_DIR=%SCRIPT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
set "LOG_FILE=%LOG_DIR%loading_quality_monitor_%TS%.log"

set "PY_EXE=python"
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" set "PY_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"

>>"%LOG_FILE%" echo [INFO] Loading quality monitor start %DATE% %TIME%
"%PY_EXE%" loading_quality_monitor.py %* >>"%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
>>"%LOG_FILE%" echo [INFO] Exit code: %EXIT_CODE%

rem Exit code 2 means anomalies were reported, not that the monitor itself failed.
if "%EXIT_CODE%"=="2" exit /b 0
exit /b %EXIT_CODE%
