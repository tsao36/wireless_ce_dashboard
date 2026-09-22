[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$FriendlyDns,
  [Parameter(Mandatory)][ValidateRange(1,65535)][int]$BackendPort
)
$ErrorActionPreference='Continue'
$out=[ordered]@{}
try {$out.Backend=(Invoke-WebRequest "http://127.0.0.1:$BackendPort" -UseBasicParsing -TimeoutSec 10).StatusCode} catch {$out.Backend="FAIL: $($_.Exception.Message)"}
try {$out.Dns=(Resolve-DnsName $FriendlyDns -ErrorAction Stop | Where-Object IPAddress | Select-Object -ExpandProperty IPAddress)-join ','} catch {$out.Dns="FAIL: $($_.Exception.Message)"}
try {$out.Http=(Invoke-WebRequest "http://$FriendlyDns" -UseBasicParsing -TimeoutSec 10).StatusCode} catch {$out.Http="FAIL: $($_.Exception.Message)"}
$out.Tcp443=(Test-NetConnection $FriendlyDns -Port 443 -InformationLevel Quiet)
try {$out.Https=(Invoke-WebRequest "https://$FriendlyDns" -UseBasicParsing -TimeoutSec 10).StatusCode} catch {$out.Https="FAIL: $($_.Exception.Message)"}
[pscustomobject]$out | Format-List
