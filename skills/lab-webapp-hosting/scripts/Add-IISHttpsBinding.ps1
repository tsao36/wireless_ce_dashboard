[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory)][string]$SiteName,
  [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9.-]+$')][string]$FriendlyDns
)
$ErrorActionPreference='Stop'
Import-Module WebAdministration
if (-not (Test-Path "IIS:\Sites\$SiteName")) { throw "IIS site '$SiteName' does not exist." }
if (Get-WebBinding -Protocol https | Where-Object bindingInformation -eq "*:443:$FriendlyDns") { throw "HTTPS binding already exists." }
$now=Get-Date
$candidates=Get-ChildItem Cert:\LocalMachine\My | Where-Object {
  $_.HasPrivateKey -and $_.NotBefore -le $now -and $_.NotAfter -gt $now -and (
    $_.DnsNameList.Unicode -contains $FriendlyDns -or $_.Subject -match "CN=$([regex]::Escape($FriendlyDns))(,|$)"
  )
}
if ($candidates.Count -ne 1) {
  $candidates | Select-Object Subject,Thumbprint,NotAfter,HasPrivateKey | Format-Table
  throw "Expected exactly one valid matching certificate; found $($candidates.Count). No binding created."
}
$cert=$candidates[0]
if ($PSCmdlet.ShouldProcess($SiteName,"Add HTTPS binding for $FriendlyDns")) {
  New-WebBinding -Name $SiteName -Protocol https -Port 443 -HostHeader $FriendlyDns -SslFlags 1
  New-Item "IIS:\SslBindings\!443!$FriendlyDns" -Thumbprint $cert.Thumbprint -SSLFlags 1 | Out-Null
}
[pscustomobject]@{Site=$SiteName;Host=$FriendlyDns;Thumbprint=$cert.Thumbprint;Expires=$cert.NotAfter;HasPrivateKey=$cert.HasPrivateKey}
