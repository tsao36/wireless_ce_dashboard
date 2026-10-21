@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "DASHBOARD_ROOT=%%~fI"
set "WIRELESS_CE_DASHBOARD_ROOT=%DASHBOARD_ROOT%"
for %%I in ("%SCRIPT_DIR%..\..\category_tuning") do set "CATEGORY_TUNING_DIR=%%~fI"
if not defined OFFLOAD_HYBRID_LLM_MIN_CONFIDENCE set "OFFLOAD_HYBRID_LLM_MIN_CONFIDENCE=0.90"
set "LOG_DIR=%SCRIPT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "PY_EXE=%DASHBOARD_ROOT%\.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

echo [INFO] Refreshing the current-year web review pool.
echo [INFO] Dashboard root: %DASHBOARD_ROOT%
echo [INFO] The team submits labels directly on category_prediction.html; no weekly spreadsheet is generated.
"%PY_EXE%" "%SCRIPT_DIR%web_feedback_cycle.py" --mode refresh-pool --dashboard-root "%DASHBOARD_ROOT%"
if errorlevel 1 (
  echo [ERROR] Web issue pool refresh failed.
  exit /b 1
)

echo [OK] Web issue pool refresh complete.
exit /b 0