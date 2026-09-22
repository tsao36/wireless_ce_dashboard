[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$FriendlyDns,
  [Parameter(Mandatory)][string]$ServerIp,
  [Parameter(Mandatory)][ValidateRange(1,65535)][int]$BackendPort
)
$ErrorActionPreference='Stop'
$result=[ordered]@{}
$result.IsAdministrator=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
try { $r=Invoke-WebRequest "http://127.0.0.1:$BackendPort" -UseBasicParsing -TimeoutSec 10; $result.BackendHttp=$r.StatusCode } catch { $result.BackendHttp="FAIL: $($_.Exception.Message)" }
$result.BackendListening=([bool](Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue))
try { $result.ReverseDns=(Resolve-DnsName $ServerIp -Type PTR -ErrorAction Stop).NameHost -join ',' } catch { $result.ReverseDns="FAIL: $($_.Exception.Message)" }
try { $result.FriendlyDns=(Resolve-DnsName $FriendlyDns -ErrorAction Stop | Where-Object IPAddress | Select-Object -ExpandProperty IPAddress) -join ',' } catch { $result.FriendlyDns="Not resolved" }
if (Test-Path "$env:windir\system32\inetsrv\appcmd.exe") {
  $result.IisBindings=& "$env:windir\system32\inetsrv\appcmd.exe" list site /text:bindings
} else { $result.IisBindings='IIS appcmd not installed' }
[pscustomobject]$result | Format-List
