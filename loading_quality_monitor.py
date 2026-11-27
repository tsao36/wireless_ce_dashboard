"""Audit a loading snapshot for data-quality and count anomalies."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from smtp_mailer import load_smtp_settings, require_sending_settings, send_messages

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
LOG = logging.getLogger("loading_quality_monitor")
DEFAULT_SNAPSHOT_PATH = ROOT / "team_loading_latest.json"
DEFAULT_REPORT_PATH = ROOT / "cache" / "loading_quality_latest.json"
DEFAULT_STATE_PATH = ROOT / "cache" / "loading_quality_notification_state.json"
TAG_PATTERN = re.compile(r"\[[^\]]*\]")
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-\d+$", re.IGNORECASE)
PLACEHOLDER_TITLES = {"", "-", "na", "n/a", "none", "null", "unknown", "(no title)"}
SUSPICIOUS_TITLE_PATTERN = re.compile(
    r"\b(please ignore|do not process|do not use|dummy|placeholder|mock issue|sample submission|"
    r"sanity test|internal test|test submission|fake issue)\b",
    re.IGNORECASE,
)
DEFAULT_OLD_ISSUE_DAYS = int(os.environ.get("LOADING_QUALITY_OLD_ISSUE_DAYS", "30"))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _issue_finding(row: dict[str, Any], owner: str, code: str, message: str, severity: str = "warning") -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "owner": owner,
        "source_type": _text(row.get("source_type")),
        "ips_case_number": _text(row.get("ips_case_number")),
        "jira_id": _text(row.get("jira_id")),
        "hsd_id": _text(row.get("hsd_id")),
        "title": _text(row.get("ips_title")),
        "category": _text(row.get("category")),
        "created": _text(row.get("created")),
        "issue_weight": row.get("issue_weight"),
    }


def inspect_title(title: Any) -> str | None:
    """Return a title-quality code, or None when the title looks descriptive."""
    value = _text(title)
    normalized = value.casefold()
    if normalized in PLACEHOLDER_TITLES:
        return "missing_title"
    if ISSUE_KEY_PATTERN.fullmatch(value) or value.isdigit():
        return "identifier_only_title"
    prose = TAG_PATTERN.sub(" ", value)
    prose = re.sub(r"[^\w]+", " ", prose, flags=re.UNICODE).strip()
    words = [word for word in prose.split() if re.search(r"[A-Za-z]", word)]
    if not words:
        return "tag_only_title"
    if len(prose) < 12 or len(words) < 3:
        return "too_little_title_detail"
    return None


def audit_snapshot(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_hours: float = 2.0,
    old_issue_days: int = DEFAULT_OLD_ISSUE_DAYS,
) -> dict[str, Any]:
    generated_at = _text(payload.get("generated_at"))
    now = now or datetime.now(timezone.utc)
    findings: list[dict[str, Any]] = []
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        findings.append({"severity": "error", "code": "missing_loading_rows", "message": "Snapshot has no loading rows."})
        rows = []

    try:
        generated = datetime.fromisoformat(generated_at)
        if generated.tzinfo is None:
            generated = generated.astimezone()
        age_hours = max(0.0, (now - generated.astimezone(timezone.utc)).total_seconds() / 3600)
        if age_hours > max_age_hours:
            findings.append({
                "severity": "warning",
                "code": "stale_snapshot",
                "message": f"Snapshot is {age_hours:.1f} hours old (limit {max_age_hours:g}).",
                "generated_at": generated_at,
            })
    except ValueError:
        findings.append({
            "severity": "error",
            "code": "invalid_generated_at",
            "message": "Snapshot generated_at is missing or invalid.",
        })

    occurrences: dict[tuple[str, str], list[str]] = defaultdict(list)
    issue_count = 0
    for row in rows:
        if not isinstance(row, dict):
            findings.append({"severity": "error", "code": "invalid_row", "message": "Loading snapshot contains a non-object row."})
            continue
        owner = _text(row.get("name")) or "(missing owner)"
        raw_issues = row.get("issue_details")
        issues = raw_issues if isinstance(raw_issues, list) else []
        if not owner.strip("()") or not issues:
            continue
        issue_count += len(issues)
        if row.get("weighted_loading_value") is not None:
            try:
                displayed_weight = float(row.get("weighted_loading_value"))
                detail_weight = round(sum(float(issue.get("issue_weight") or 0.0) for issue in issues if isinstance(issue, dict)), 2)
                if abs(displayed_weight - detail_weight) > 0.011:
                    findings.append({
                        "severity": "error",
                        "code": "weighted_load_mismatch",
                        "message": f"Displayed weighted load {displayed_weight:.2f} differs from issue sum {detail_weight:.2f}.",
                        "owner": owner,
                    })
            except (TypeError, ValueError):
                findings.append({
                    "severity": "error",
                    "code": "invalid_weighted_loading",
                    "message": "Displayed weighted loading or one issue weight is invalid.",
                    "owner": owner,
                })
        if all(key in row for key in ("active_uips", "jira", "active_hsd")):
            expected_count = int(row.get("active_uips") or 0) + int(row.get("jira") or 0) + int(row.get("active_hsd") or 0)
            if len(issues) != expected_count:
                findings.append({
                    "severity": "error",
                    "code": "issue_count_mismatch",
                    "message": f"Issue details contain {len(issues)} entries but active counts total {expected_count}.",
                    "owner": owner,
                })
        for issue in issues:
            if not isinstance(issue, dict):
                findings.append({"severity": "error", "code": "invalid_issue", "message": "Issue list contains a non-object item.", "owner": owner})
                continue
            title_code = inspect_title(issue.get("ips_title"))
            if title_code:
                messages = {
                    "missing_title": "Issue title is missing or a placeholder.",
                    "identifier_only_title": "Issue title contains only an identifier.",
                    "tag_only_title": "Issue title contains tags/identifiers but no problem description.",
                    "too_little_title_detail": "Issue title contains too little descriptive detail to classify safely.",
                }
                findings.append(_issue_finding(issue, owner, title_code, messages[title_code]))
            title = _text(issue.get("ips_title"))
            if SUSPICIOUS_TITLE_PATTERN.search(title):
                findings.append(_issue_finding(
                    issue,
                    owner,
                    "suspicious_non_customer_title",
                    "Title suggests a test, placeholder, or instruction that may not represent a customer submission.",
                ))
            created_text = _text(issue.get("created"))
            try:
                created = datetime.fromisoformat(created_text)
                created_date = created.date() if created.tzinfo else datetime.fromisoformat(created_text).date()
                issue_age_days = (now.date() - created_date).days
                if issue_age_days > old_issue_days:
                    finding = _issue_finding(
                        issue,
                        owner,
                        "old_active_issue",
                        f"Issue remains active after {issue_age_days} days (limit {old_issue_days}); verify status, ownership, and continued weighting.",
                    )
                    finding["age_days"] = issue_age_days
                    findings.append(finding)
            except (TypeError, ValueError):
                if title:
                    findings.append(_issue_finding(issue, owner, "invalid_issue_date", "Issue created date is missing or invalid."))
            identifiers = [
                ("ips", _text(issue.get("ips_case_number"))),
                ("jira", _text(issue.get("jira_id"))),
                ("hsd", _text(issue.get("hsd_id"))),
            ]
            ips_case_number = identifiers[0][1]
            ips_url = _text(issue.get("ips_url"))
            if ips_case_number and not re.match(r"^https://[^/]+", ips_url, re.IGNORECASE):
                findings.append(_issue_finding(
                    issue,
                    owner,
                    "missing_ips_url",
                    "IPS issue is missing a valid HTTPS source URL, so its dashboard identifier cannot be linked.",
                ))
            if not any(value for _kind, value in identifiers):
                findings.append(_issue_finding(issue, owner, "missing_issue_identifier", "Issue has no IPS, Jira, or HSD identifier."))
            for kind, value in identifiers:
                if value:
                    occurrences[(kind, value)].append(owner)
            try:
                weight = float(issue.get("issue_weight"))
                if weight <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                findings.append(_issue_finding(issue, owner, "invalid_issue_weight", "Issue weight is missing or not positive.", "error"))

    for (kind, value), owners in sorted(occurrences.items()):
        if len(owners) > 1:
            findings.append({
                "severity": "error",
                "code": "duplicate_issue_assignment",
                "message": f"{kind.upper()} {value} appears under multiple loading entries: {', '.join(owners)}.",
                "owners": owners,
                "identifier": value,
            })

    severity_rank = {"info": 0, "warning": 1, "error": 2}
    findings.sort(key=lambda item: (-severity_rank.get(str(item.get("severity")), 0), str(item.get("code")), str(item.get("owner"))))
    return {
        "checked_at": now.isoformat(),
        "snapshot_generated_at": generated_at,
        "snapshot_rows": len(rows),
        "issue_count": issue_count,
        "status": "ok" if not findings else ("error" if any(item.get("severity") == "error" for item in findings) else "warning"),
        "findings": findings,
        "ai_review": [],
    }


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def _review_with_windows_transport(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    script = r"""
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
[Net.ServicePointManager]::ServerCertificateValidationCallback = {
    param($sender, $certificate, $chain, $errors)
    if (($errors -band [Net.Security.SslPolicyErrors]::RemoteCertificateNameMismatch) -ne 0) { return $false }
    if (($errors -band [Net.Security.SslPolicyErrors]::RemoteCertificateNotAvailable) -ne 0) { return $false }
    $expectedRoot = '8F119801FCA4C259089C1A81AF5C6832C8085061'
    return [bool]($chain.ChainElements | Where-Object { $_.Certificate.Thumbprint -eq $expectedRoot } | Select-Object -First 1)
}
$request = [Net.HttpWebRequest]::Create($env:GNAI_REQUEST_URL)
$request.Method = 'POST'
$request.ContentType = 'application/json'
$request.Accept = 'application/json'
$request.Headers['Authorization'] = 'Bearer ' + $env:GNAI_TOKEN
$request.Proxy = $null
$request.Timeout = 90000
$request.ReadWriteTimeout = 90000
$body = [Text.Encoding]::UTF8.GetBytes([Console]::In.ReadToEnd())
$request.ContentLength = $body.Length
$stream = $request.GetRequestStream()
try { $stream.Write($body, 0, $body.Length) } finally { $stream.Dispose() }
$response = $request.GetResponse()
try {
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    try { [Console]::Out.Write($reader.ReadToEnd()) } finally { $reader.Dispose() }
} finally { $response.Dispose() }
"""
    environment = os.environ.copy()
    environment["GNAI_REQUEST_URL"] = url
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=100,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Windows GNAI transport failed: {result.stderr.strip()[:300]}")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Windows GNAI transport returned invalid JSON: {result.stdout[:200]}") from exc
    return response if isinstance(response, dict) else {}


def _normalize_ai_reviews(items: Any) -> list[dict[str, Any]]:
    recommendations = {"retain", "investigate", "remove", "reweight"}
    severities = {"info", "warning", "error"}
    normalized: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        if not isinstance(item, dict):
            continue
        recommendation = _text(item.get("recommendation")).casefold()
        severity = _text(item.get("severity")).casefold()
        if recommendation not in recommendations or severity not in severities:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        normalized.append({
            "ips_case_number": _text(item.get("ips_case_number")),
            "jira_id": _text(item.get("jira_id")),
            "severity": severity,
            "recommendation": recommendation,
            "confidence": confidence,
            "reason": _text(item.get("reason")),
        })
    return normalized


def _ai_candidates(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for item in findings:
        if item.get("code") in {"stale_snapshot", "invalid_generated_at", "missing_loading_rows"}:
            continue
        key = (
            _text(item.get("owner")),
            _text(item.get("ips_case_number")),
            _text(item.get("jira_id")),
            _text(item.get("hsd_id")),
            _text(item.get("title")),
        )
        if key not in grouped:
            grouped[key] = {**item, "signals": []}
        grouped[key]["signals"].append({
            "code": item.get("code"),
            "severity": item.get("severity"),
            "message": item.get("message"),
        })
    return list(grouped.values())


def review_with_ai(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    api_key = _text(os.environ.get("GNAI_TOKEN"))
    if not api_key:
        raise RuntimeError("GNAI_TOKEN is required for AI review")
    candidates = _ai_candidates(findings)
    if not candidates:
        return []
    base_url = _text(os.environ.get("GNAI_URL")) or "https://gnai.intel.com/api/providers/openai/v1"
    model = _text(os.environ.get("LOADING_QUALITY_AI_MODEL")) or _text(os.environ.get("GNAI_MODEL")) or "gpt-4.1"
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You audit Wireless CE loading data for likely faulty entries and incorrect weighting. "
                    "Treat deterministic findings as signals, not proof. Review title, category, age, owner, "
                    "source type, identifiers, and weight. A title containing test/sanity/sample/ignore language "
                    "may be non-customer work; an old active issue may still be valid, so do not recommend removal "
                    "from age alone. Return JSON with an 'items' array. Each item must include "
                    "'ips_case_number', 'jira_id', 'severity' (info/warning/error), "
                    "'recommendation' (retain/investigate/remove/reweight), 'confidence' (0 to 1), and 'reason'. "
                    "Recommend remove only for strong evidence; recommend investigate for uncertain old or test-like cases. "
                    "For reweight, say which category/weight evidence appears inconsistent. Be concise, do not invent facts, "
                    "and do not claim that an issue is closed or invalid without evidence."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"findings": candidates}, ensure_ascii=True),
            },
        ],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if ".intel.com" in base_url else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=90) as response:
            content = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"AI review failed with HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}") from exc
    except urllib.error.URLError as exc:
        error_text = str(exc)
        if os.name != "nt" or "certificate" not in error_text.casefold():
            raise RuntimeError(f"AI review transport failed: {error_text}") from exc
        content = _review_with_windows_transport(base_url.rstrip("/") + "/chat/completions", payload)
    choices = content.get("choices") or []
    raw = _safe_json(_text((choices[0].get("message") or {}).get("content")) if choices else "")
    items = raw.get("items") if isinstance(raw, dict) else []
    return _normalize_ai_reviews(items)


def finding_fingerprint(report: dict[str, Any]) -> str:
    stable = sorted(
        (item.get("code"), item.get("severity"), item.get("owner"), item.get("ips_case_number"), item.get("jira_id"), item.get("hsd_id"))
        for item in report.get("findings", [])
    )
    return hashlib.sha256(json.dumps(stable, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def write_report(report: dict[str, Any], path: Path = DEFAULT_REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_reporter_email_map(path: Path = ROOT / "recipients.json") -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        _text(name).casefold(): _text(email)
        for name, email in (payload.get("reporter_email_map") or {}).items()
        if "@" in _text(email)
    }


def _review_belongs_to_finding(review: dict[str, Any], finding: dict[str, Any]) -> bool:
    for key in ("ips_case_number", "jira_id", "hsd_id"):
        review_value = _text(review.get(key))
        finding_value = _text(finding.get(key))
        if review_value and finding_value and review_value == finding_value:
            return True
    return False


def _owner_reports(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in report.get("findings", []):
        grouped[_text(finding.get("owner"))].append(finding)
    reports: list[tuple[str, dict[str, Any]]] = []
    for owner, findings in sorted(grouped.items(), key=lambda item: item[0].casefold()):
        reviews = [
            review
            for review in report.get("ai_review", [])
            if any(_review_belongs_to_finding(review, finding) for finding in findings)
        ]
        reports.append((owner, {**report, "findings": findings, "ai_review": reviews}))
    return reports


def _email_report(report: dict[str, Any], recipients: list[str], *, supervisor: str = "") -> None:
    settings = load_smtp_settings()
    require_sending_settings(settings)
    lines = [
        f"Loading quality monitor: {report['status'].upper()}",
        f"Snapshot: {report.get('snapshot_generated_at') or '(unknown)'}",
        f"Issues checked: {report.get('issue_count', 0)}",
        "",
    ]
    for finding in report.get("findings", []):
        identity = finding.get("ips_case_number") or finding.get("jira_id") or finding.get("hsd_id") or "snapshot"
        lines.append(f"- [{finding.get('severity')}] {identity}: {finding.get('message')}")
    for review in report.get("ai_review", []):
        identity = review.get("ips_case_number") or review.get("jira_id") or "snapshot"
        recommendation = str(review.get("recommendation") or "investigate").upper()
        confidence = review.get("confidence")
        confidence_text = f" ({float(confidence):.0%})" if isinstance(confidence, (int, float)) else ""
        lines.append(f"- [AI {recommendation}{confidence_text}] {identity}: {review.get('reason')}")
    message = EmailMessage()
    owner = _text((report.get("findings") or [{}])[0].get("owner"))
    message["Subject"] = f"Wireless CE loading quality: {owner or report['status'].upper()}"
    message["From"] = settings["SMTP_FROM"]
    unique_recipients = list(dict.fromkeys(address.strip() for address in recipients if "@" in address.strip()))
    message["To"] = ", ".join(unique_recipients)
    if supervisor and supervisor.casefold() not in {address.casefold() for address in unique_recipients}:
        message["Cc"] = supervisor
    message.set_content("\n".join(lines))
    send_messages([message], settings)


def _send_anomaly_emails(report: dict[str, Any], recipient_override: str = "") -> None:
    supervisor = _text(os.environ.get("LOADING_QUALITY_SUPERVISOR_EMAIL")) or "jonathan.tsao@intel.com"
    if recipient_override:
        _email_report(report, [recipient_override], supervisor=supervisor)
        return
    email_map = _load_reporter_email_map()
    for owner, owner_report in _owner_reports(report):
        engineer = email_map.get(owner.casefold())
        recipients = [engineer] if engineer else []
        if supervisor.casefold() not in {address.casefold() for address in recipients}:
            recipients.append(supervisor)
        if not recipients:
            continue
        _email_report(owner_report, recipients)


def run_monitor(
    snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    enable_ai: bool | None = None,
    enable_email: bool | None = None,
    force_email: bool = False,
    email: str = "",
    max_age_hours: float = 2.0,
) -> dict[str, Any]:
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Loading snapshot must be a JSON object")
    report = audit_snapshot(payload, max_age_hours=max_age_hours)
    fingerprint = finding_fingerprint(report)
    old_fingerprint = ""
    if state_path.is_file():
        try:
            old_fingerprint = _text(json.loads(state_path.read_text(encoding="utf-8")).get("fingerprint"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    ai_enabled = enable_ai if enable_ai is not None else _text(os.environ.get("LOADING_QUALITY_AI_ENABLED")).casefold() in {"1", "true", "yes"}
    if ai_enabled and fingerprint == old_fingerprint and report_path.is_file():
        try:
            previous_report = json.loads(report_path.read_text(encoding="utf-8"))
            report["ai_review"] = previous_report.get("ai_review") or []
            if previous_report.get("ai_error"):
                report["ai_error"] = previous_report["ai_error"]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    elif ai_enabled and report["findings"]:
        try:
            report["ai_review"] = review_with_ai(report["findings"])
        except Exception as exc:
            report["ai_review"] = []
            report["ai_error"] = f"{type(exc).__name__}: {exc}"
            LOG.warning("AI review unavailable: %s", exc)
    write_report(report, report_path)

    email_enabled = enable_email if enable_email is not None else _text(os.environ.get("LOADING_QUALITY_EMAIL_ALERTS")).casefold() in {"1", "true", "yes"}
    if email_enabled and (force_email or fingerprint != old_fingerprint) and report["status"] != "ok":
        _send_anomaly_emails(report, email or _text(os.environ.get("LOADING_QUALITY_ALERT_TO")))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"fingerprint": fingerprint, "updated_at": report["checked_at"]}, indent=2), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--email", default="", help="Override all anomaly recipients for testing; still includes Jonathan by Cc unless he is the recipient.")
    parser.add_argument("--no-email", action="store_true", help="Audit and write the report without sending anomaly emails.")
    parser.add_argument("--force-email", action="store_true", help="Send current anomalies even when the finding set has not changed.")
    parser.add_argument("--ai", action="store_true", help="Enable optional AI review for deterministic findings.")
    parser.add_argument("--max-age-hours", type=float, default=float(os.environ.get("LOADING_QUALITY_MAX_AGE_HOURS", "2")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        report = run_monitor(
            args.snapshot,
            args.report,
            enable_ai=args.ai or None,
            enable_email=False if args.no_email else (True if args.email or args.force_email else None),
            force_email=args.force_email,
            email=args.email,
            max_age_hours=args.max_age_hours,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("Loading quality monitor failed: %s", exc)
        return 1
    LOG.info("Loading quality %s: %d finding(s), report=%s", report["status"], len(report["findings"]), args.report)
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
