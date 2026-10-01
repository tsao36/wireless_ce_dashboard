@echo off
setlocal
cd /d "%~dp0"

rem The backend task runs as SYSTEM, so stopping/starting it needs admin rights.
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Restarting Wireless CE Dashboard backend...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_dashboard.ps1"
if errorlevel 1 (
    echo [ERROR] Restart failed. See logs\restart_dashboard.log
    pause
    exit /b 1
)

echo Waiting for the dashboard to come back on port 8010...
powershell -NoProfile -Command "for ($i = 0; $i -lt 30; $i++) { try { if ((Invoke-WebRequest http://127.0.0.1:8010/api/dates -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) { Write-Host '[OK] Dashboard is up.'; Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Where-Object CommandLine -match 'loading_dashboard' | Select-Object ProcessId, CreationDate, CommandLine | Format-List; exit 0 } } catch {}; Start-Sleep -Seconds 3 }; Write-Host '[ERROR] Dashboard did not respond within 90 seconds. Check logs\loading_dashboard.log'; exit 1"

pause
