# Reference Runbook

## Chinese IIS labels

- Sites: 網站
- Add Website: 新增網站
- Bindings: 繫結
- Site Bindings: 網站繫結
- Add: 新增
- Server Certificates: 伺服器憑證
- Import: 匯入
- Application Request Routing Cache: 應用程式要求路由快取
- Server Proxy Settings: 伺服器 Proxy 設定
- Enable Proxy: 啟用 Proxy
- URL Rewrite: URL Rewrite
- Actions: 動作
- Apply: 套用

## Known-good example

- Friendly DNS: `wireless-training-scheduler.intel.com`
- Server FQDN: `DESKTOP-KHH0N6B.itwn.intel.com`
- Server IP: `10.225.74.147`
- Backend: `http://127.0.0.1:3001`
- IIS ports: HTTP 80, HTTPS 443

## Troubleshooting matrix

### ARR 502.3 and 0x80072ee7

Cause: malformed or unresolvable rewrite target. Check for `http://http://`.

Correct target:

```text
http://127.0.0.1:<backendPort>/{R:1}
```

### ARR connection refused

- Confirm the application process is running.
- Run `Get-NetTCPConnection -LocalPort <backendPort> -State Listen`.
- Test `Invoke-WebRequest http://127.0.0.1:<backendPort>`.

### HTTP 404

- Check the IIS hostname binding.
- Check that the request reaches the intended site rather than Default Web Site.
- Check URL Rewrite match pattern and action.

### HTTP 503

- Confirm the IIS site and application pool are started.
- Review Windows Event Viewer and IIS logs.

### DNS failure

- Verify the CNAME name, DNS zone, target FQDN, and DDI request status.
- Run `Resolve-DnsName <friendlyDns>` from both server and client.

### TCP 443 listens but TLS fails

- Confirm HTTPS binding selects the intended certificate.
- Confirm the certificate is under Local Computer, not Current User.
- Confirm `HasPrivateKey` is true.
- Confirm CN/SAN contains the friendly DNS name.
- Confirm the certificate is within its validity period.
- Test from a current browser if legacy Windows PowerShell reports a TLS negotiation error.

### API or WebSocket failures

- Remove hard-coded client URLs that include the backend port.
- Prefer relative API URLs.
- Check whether WebSocket support is required and enabled.
- Check forwarded host/protocol handling in the application.

## Operational handoff

- Configure the web app to start after reboot without interactive login.
- Record app location, start command, port, logs, version, rollback method, and owner.
- Assign DNS, server, and certificate primary/backup owners.
- Track certificate expiration. Do not assume renewal is automatic when Certificate Manager shows Automatic Renewal Disabled.
- After IIS is stable, restrict direct client access to the backend port where practical.
