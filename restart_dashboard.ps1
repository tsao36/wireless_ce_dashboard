# Restarts the dashboard backend; launched detached by the "Restart server" button.
$ErrorActionPreference = 'Continue'
$taskName = 'Wireless CE Dashboard Backend'
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir 'restart_dashboard.log'

function Write-Log([string]$message) {
    Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
}

if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Log "Scheduled task '$taskName' not found; nothing restarted."
    exit 1
}

# Let the HTTP response reach the browser before the backend is stopped.
Start-Sleep -Seconds 2
Write-Log 'Restart requested.'

Stop-ScheduledTask -TaskName $taskName
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object CommandLine -match 'loading_dashboard' |
    ForEach-Object {
        Write-Log "Stopping PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName $taskName
Write-Log 'Scheduled task started.'
