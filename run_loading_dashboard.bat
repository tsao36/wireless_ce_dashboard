@echo off
setlocal

cd /d "%~dp0"

for %%I in ("%~dp0.") do set "WIRELESS_CE_DASHBOARD_ROOT=%%~fI"
for %%I in ("%~dp0..\category_tuning") do set "CATEGORY_TUNING_DIR=%%~fI"
if not defined OFFLOAD_HYBRID_LLM_MIN_CONFIDENCE set "OFFLOAD_HYBRID_LLM_MIN_CONFIDENCE=0.90"
set "SSL_CERT_FILE=C:\ProgramData\Wirelessce\certs\intel-ca-bundle.pem"

set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "PY=python"
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if exist ".\.venv\Scripts\python.exe" (
	.\.venv\Scripts\python.exe --version >nul 2>nul
	if not errorlevel 1 (
		set "PY=.\.venv\Scripts\python.exe"
	)
)

:loop
echo [INFO] %DATE% %TIME% Starting loading_dashboard.py on port 8010 >>"%LOG_DIR%\loading_dashboard.log"
%PY% loading_dashboard.py --port 8010 >>"%LOG_DIR%\loading_dashboard.log" 2>&1
echo [WARN] %DATE% %TIME% loading_dashboard.py exited with code %ERRORLEVEL%, restarting in 5s >>"%LOG_DIR%\loading_dashboard.log"
timeout /t 5 /nobreak >nul
goto loop

endlocal
