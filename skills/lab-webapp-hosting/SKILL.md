---
name: lab-webapp-hosting
description: Publish an internal web application from a Windows lab server using an internal DNS CNAME, IIS, URL Rewrite, ARR reverse proxy, and an internal TLS certificate. Use when asked to give a lab-hosted web app a friendly hostname, remove a visible application port, configure IIS reverse proxy, troubleshoot ARR 502 errors, request a server certificate, or enable HTTPS.
compatibility: Windows Server or Windows client with administrative PowerShell; IIS; Intel internal network and authorized access to DDI and Certificate Manager. Portal steps may require interactive user authentication and approval.
metadata:
  author: jonathan-tsao
  version: "1.0.0"
---

# Lab Web App Hosting

Publish a locally running web application through a friendly internal DNS name and IIS HTTPS endpoint.

## Required inputs

Collect these values before changing the system:

- `friendlyDns`: user-facing FQDN, for example `wireless-training-scheduler.intel.com`
- `serverIp`: lab server IP
- `serverFqdn`: existing FQDN returned by reverse DNS
- `backendPort`: local application port, for example `3001`
- `siteName`: IIS site name
- `certificateNickname`: Certificate Manager object nickname

Never request or display a PFX password, private key, account password, or other secret in chat or logs.

## Safety boundaries

- Treat DNS and Certificate Manager as authenticated enterprise workflows. Do not submit, approve, delete, or replace records unless the user explicitly asks and the required browser or enterprise tool is available.
- Do not expose the lab server publicly.
- Before modifying IIS, inspect existing sites and bindings to avoid port or hostname collisions.
- Do not overwrite an existing IIS site or binding. Stop and report the conflict.
- Do not add HTTP-to-HTTPS redirect until HTTPS validation passes.
- Keep the backend bound to loopback where possible and do not open the backend port broadly unless required.
- Back up IIS configuration before modifications:

```powershell
& "$env:windir\system32\inetsrv\appcmd.exe" add backup "before-lab-webapp-hosting"
```

## Workflow

### 1. Validate the application and resolve the server identity

Run the preflight script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Test-LabWebAppPrereqs.ps1 `
  -FriendlyDns "<friendlyDns>" `
  -ServerIp "<serverIp>" `
  -BackendPort <backendPort>
```

Required pass conditions:

- `http://127.0.0.1:<backendPort>` returns an HTTP response.
- `<backendPort>` is listening.
- Reverse DNS for `<serverIp>` returns `<serverFqdn>`.
- Existing IIS bindings do not already claim the same hostname and port.
- The requested friendly hostname resolves in internal DNS before trying browser validation from another system.

Important operational note: if the local app responds on `127.0.0.1` or the server IP but a client gets `HTTP Error 400. The request hostname is invalid.` or `Bad Request - Invalid Hostname`, treat this as a host-header/DNS publication issue, not an app failure. In those cases, verify the host name exists in internal DNS and that the IIS binding matches the exact hostname or wildcard pattern in use.

### 2. Create internal DNS alias

Use the DDI Self Service Portal to create an Internal DNS Alias/CNAME:

- Alias/Name: host portion of `<friendlyDns>`
- Zone: matching internal DNS zone
- Record type: CNAME/Alias
- Target: `<serverFqdn>`
- Owner: service owner or team PDL

Do not create a second A record when the server already has a stable FQDN. After processing, verify:

```powershell
Resolve-DnsName <friendlyDns>
```

The resolved chain must end at `<serverIp>`.

### 3. Install and enable IIS reverse-proxy components

Run from elevated PowerShell:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName IIS-WebServerRole -All
winget install --id Microsoft.IIS.ApplicationRequestRouting --exact --accept-package-agreements --accept-source-agreements
& "$env:windir\system32\inetsrv\appcmd.exe" set config -section:system.webServer/proxy /enabled:"True" /commit:apphost
iisreset
```

The ARR package includes or depends on URL Rewrite. Verify the modules are present before continuing.

### 4. Configure the IIS site and reverse proxy

Use the configuration script only after preflight passes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Configure-IISReverseProxy.ps1 `
  -SiteName "<siteName>" `
  -FriendlyDns "<friendlyDns>" `
  -BackendPort <backendPort>
```

The resulting inbound rewrite target must be exactly:

```text
http://127.0.0.1:<backendPort>/{R:1}
```

Never create `http://http://...`. That malformed target causes ARR 502.3 with error `0x80072ee7`.

Validate HTTP:

```powershell
Invoke-WebRequest "http://<friendlyDns>" -UseBasicParsing
```

### 5. Request the internal server certificate

In Certificate Manager, use the SSL server-certificate request workflow.

- Request folder: SSL web-server request folder, such as `Policy\Certificates\SSL\_NewRequest`
- Certificate Authority: `Intel SSL Internal SHA2`
- Common Name: `<friendlyDns>`
- SAN DNS: `<friendlyDns>`
- SAN IP: omit unless policy explicitly requires it
- Algorithm: RSA 2048, unless current policy specifies another value
- Highwire integration: False when the site is not behind F5 Highwire
- CertAdmins: primary and backup service owners

Do not select Client Authentication, mTLS ClientAuth, VPN, or Workstation Authentication templates for an IIS server endpoint.

### 6. Import and bind the certificate

The certificate must be in `Local Computer\Personal` and include its private key. Then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Add-IISHttpsBinding.ps1 `
  -SiteName "<siteName>" `
  -FriendlyDns "<friendlyDns>"
```

The script must select an unexpired certificate whose DNS name matches `<friendlyDns>` and which has a private key. If none or multiple are found, stop and report the candidates.

Validate:

```powershell
Test-NetConnection <friendlyDns> -Port 443
Invoke-WebRequest "https://<friendlyDns>" -UseBasicParsing
```

### 7. Add redirect only after HTTPS passes

Add an HTTP-to-HTTPS redirect only after the HTTPS request succeeds. Use:

- Match: `(.*)`
- Condition input: `{HTTPS}`
- Condition pattern: `^OFF$`
- Redirect: `https://{HTTP_HOST}/{R:1}`
- Status: Permanent 301

### 8. Final verification

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Test-PublishedWebApp.ps1 `
  -FriendlyDns "<friendlyDns>" `
  -BackendPort <backendPort>
```

Do not declare success unless local backend, DNS, HTTP, TCP 443, HTTPS, and certificate-name checks pass.

## Troubleshooting

Load `references/runbook.md` for error-specific diagnosis, Chinese IIS UI labels, certificate checks, and operational handoff requirements.

## Output format

Report:

1. Inputs used
2. Preflight result
3. DNS state
4. IIS/ARR state
5. HTTP result
6. Certificate state without secret material
7. HTTPS result
8. Any remaining manual approval or portal action
