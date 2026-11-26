param(
    [ValidatePattern("^([01]\d|2[0-3]):[0-5]\d$")]
    [string]$Time = "08:00"
)

$ErrorActionPreference = "Stop"
$TaskName = "Wireless CE Daily Loading Summary"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Action = New-ScheduledTaskAction -Execute (Join-Path $ScriptRoot "run_offload_loading_summary_daily.bat") `
    -WorkingDirectory $ScriptRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings -Force | Out-Null
Write-Host "Registered '$TaskName' daily at $Time."
Write-Host "The first run may require Microsoft sign-in for delegated Graph Mail.Send permission."
