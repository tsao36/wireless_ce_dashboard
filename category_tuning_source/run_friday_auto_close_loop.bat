@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
for %%I in ("%SCRIPT_DIR%..") do set "DASHBOARD_ROOT=%%~fI"
set "WIRELESS_CE_DASHBOARD_ROOT=%DASHBOARD_ROOT%"
set "PROJECT_ROOT=%SCRIPT_DIR%..\..\category_tuning"
set "CATEGORY_TUNING_DIR=%PROJECT_ROOT%"
if not defined OFFLOAD_HYBRID_LLM_MIN_CONFIDENCE set "OFFLOAD_HYBRID_LLM_MIN_CONFIDENCE=0.90"
set "TUNING_ROOT=%PROJECT_ROOT%\tuning_outputs"
set "WEEKLY_OUTPUT_DIR=%PROJECT_ROOT%\CFE_input"
set "INPUT_DIRS=%PROJECT_ROOT%\CFE_reviewed_issue,%PROJECT_ROOT%\CFE_input"
set "ACTIVE_MODEL=%PROJECT_ROOT%\models\issue_category_model.joblib"
set "ACTIVE_METRICS=%PROJECT_ROOT%\models\issue_category_model_metrics.json"
set "CANDIDATE_DIR=%PROJECT_ROOT%\models\candidates"
set "ARCHIVE_DIR=%PROJECT_ROOT%\models\archive"

set "PY_EXE=%DASHBOARD_ROOT%\.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

set "LOG_DIR=%SCRIPT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
if "%TS%"=="" set "TS=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "LOG_FILE=%LOG_DIR%\friday_auto_close_loop_%TS%.log"

REM Recommended scheduler command (no arguments):
REM   .\run_friday_auto_close_loop.bat
REM Recommended defaults:
REM   min_new_labels=10, min_gain=0.005, key_recall_drop_tolerance=0.02, threshold=0.45, allow_partial=1

set "MIN_NEW_LABELS=10"
set "MIN_GAIN=0.005"
set "KEY_DROP_TOL=0.02"
set "THRESHOLD=0.45"
set "ALLOW_PARTIAL=1"

if not "%~1"=="" set "MIN_NEW_LABELS=%~1"
if not "%~2"=="" set "MIN_GAIN=%~2"
if not "%~3"=="" set "KEY_DROP_TOL=%~3"
if not "%~4"=="" set "THRESHOLD=%~4"
if /i "%~5"=="--allow-partial" set "ALLOW_PARTIAL=1"

(
echo [INFO] ===== Friday auto close-loop start =====
echo [INFO] Timestamp: %DATE% %TIME%
echo [INFO] Script dir: %SCRIPT_DIR%
echo [INFO] Project root: %PROJECT_ROOT%
echo [INFO] tuning_root=%TUNING_ROOT%
echo [INFO] weekly_output_dir=%WEEKLY_OUTPUT_DIR%
echo [INFO] input_dirs=%INPUT_DIRS%
echo [INFO] min_new_labels=%MIN_NEW_LABELS% min_gain=%MIN_GAIN% key_recall_drop_tol=%KEY_DROP_TOL% threshold=%THRESHOLD% allow_partial=%ALLOW_PARTIAL%
echo [INFO] This batch does not send email notifications. It only writes result artifacts.
)>>"%LOG_FILE%"

echo [INFO] Friday recommended command: .\run_friday_auto_close_loop.bat
echo [INFO] Equivalent full args: .\run_friday_auto_close_loop.bat 10 0.005 0.02 0.45 --allow-partial
echo [INFO] Log file: %LOG_FILE%
echo [INFO] Python exe: %PY_EXE% %PY_ARGS%

echo [INFO] Friday auto close-loop starting...
echo [INFO] min_new_labels=%MIN_NEW_LABELS% min_gain=%MIN_GAIN% key_recall_drop_tol=%KEY_DROP_TOL% threshold=%THRESHOLD% allow_partial=%ALLOW_PARTIAL%

echo [INFO] Friday reads direct webpage feedback; no weekly spreadsheet precheck is used.
echo [INFO] Friday reads direct webpage feedback; no weekly spreadsheet precheck is used.>>"%LOG_FILE%"

echo [STEP 2/2] Run Friday auto close-loop
echo [STEP 2/2] Run Friday auto close-loop>>"%LOG_FILE%"
"%PY_EXE%" "%SCRIPT_DIR%web_feedback_cycle.py" --mode retrain --dashboard-root "%DASHBOARD_ROOT%" --min-new-labels %MIN_NEW_LABELS% >>"%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [ERROR] Friday auto close-loop failed.
  echo [ERROR] Friday auto close-loop failed.>>"%LOG_FILE%"
  echo [INFO] Exit code: 1>>"%LOG_FILE%"
  echo [INFO] ===== Friday auto close-loop end =====>>"%LOG_FILE%"
  exit /b 1
)

echo [OK] Friday auto close-loop completed.
echo [OK] Check latest weekly folder for model_promotion_decision.json
echo [OK] Friday auto close-loop completed.>>"%LOG_FILE%"
echo [OK] Check latest weekly folder for model_promotion_decision.json>>"%LOG_FILE%"
echo [INFO] No email sent by design in this batch. If needed, add a post-step email script.>>"%LOG_FILE%"
echo [INFO] Exit code: 0>>"%LOG_FILE%"
echo [INFO] ===== Friday auto close-loop end =====>>"%LOG_FILE%"
exit /b 0
