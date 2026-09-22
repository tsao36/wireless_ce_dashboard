#Requires -RunAsAdministrator
# Registers a Scheduled Task that starts the loading_dashboard.py backend (port 8010) at every system boot.
$action = New-ScheduledTaskAction -Execute 'C:\Wireless_CFE_App\wireless_ce_dashboard\run_loading_dashboard.bat' `
    -WorkingDirectory 'C:\Wireless_CFE_App\wireless_ce_dashboard'
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden

Register-ScheduledTask -TaskName 'Wireless CE Dashboard Backend' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force

Start-ScheduledTask -TaskName 'Wireless CE Dashboard Backend'
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName 'Wireless CE Dashboard Backend' | Get-ScheduledTaskInfo
Get-NetTCPConnection -LocalPort 8010 -ErrorAction SilentlyContinue
