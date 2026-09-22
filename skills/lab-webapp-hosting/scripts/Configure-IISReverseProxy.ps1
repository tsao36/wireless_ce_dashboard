[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]+$')][string]$SiteName,
  [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9.-]+$')][string]$FriendlyDns,
  [Parameter(Mandatory)][ValidateRange(1,65535)][int]$BackendPort,
  [string]$PhysicalPath='C:\inetpub\wwwroot'
)
$ErrorActionPreference='Stop'
Import-Module WebAdministration
if (-not (Test-Path $PhysicalPath)) { New-Item -ItemType Directory -Path $PhysicalPath -Force | Out-Null }
$conflicts=Get-WebBinding | Where-Object { $_.bindingInformation -eq "*:80:$FriendlyDns" }
if ($conflicts) { throw "HTTP binding *:80:$FriendlyDns already exists. No changes made." }
if (Test-Path "IIS:\Sites\$SiteName") { throw "IIS site '$SiteName' already exists. No changes made." }
$appcmd="$env:windir\system32\inetsrv\appcmd.exe"
& $appcmd add backup "before-$SiteName" | Out-Null
& $appcmd set config -section:system.webServer/proxy /enabled:"True" /commit:apphost | Out-Null
if ($PSCmdlet.ShouldProcess($SiteName,'Create IIS site and reverse proxy rule')) {
  New-Website -Name $SiteName -PhysicalPath $PhysicalPath -Port 80 -HostHeader $FriendlyDns | Out-Null
  $filter="/system.webServer/rewrite/rules/rule[@name='ReverseProxyToBackend']"
  Add-WebConfigurationProperty -PSPath "IIS:\Sites\$SiteName" -Filter 'system.webServer/rewrite/rules' -Name '.' -Value @{name='ReverseProxyToBackend';stopProcessing='True'}
  Set-WebConfigurationProperty -PSPath "IIS:\Sites\$SiteName" -Filter "$filter/match" -Name 'url' -Value '(.*)'
  Set-WebConfigurationProperty -PSPath "IIS:\Sites\$SiteName" -Filter "$filter/action" -Name 'type' -Value 'Rewrite'
  Set-WebConfigurationProperty -PSPath "IIS:\Sites\$SiteName" -Filter "$filter/action" -Name 'url' -Value "http://127.0.0.1:$BackendPort/{R:1}"
  Start-Website $SiteName
}
Get-Website -Name $SiteName | Format-List Name,State,PhysicalPath,Bindings
