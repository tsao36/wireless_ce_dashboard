#Requires -RunAsAdministrator
param(
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"
$TaskName = "Wireless CE Loading Quality Monitor"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Action = New-ScheduledTaskAction -Execute (Join-Path $ScriptRoot "run_loading_quality_monitor.bat") -WorkingDirectory $ScriptRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(8) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType InteractiveToken
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings -Force | Out-Null
Write-Host "Registered '$TaskName' every $IntervalMinutes minute(s)."
Write-Host "Logs: $(Join-Path $ScriptRoot 'logs\loading_quality_monitor_*.log')"
