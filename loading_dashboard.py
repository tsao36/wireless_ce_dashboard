"""Serve the latest weighted team loading snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import psycopg2
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from psycopg2 import sql

from APIs import Sherlock

ROOT = Path(__file__).resolve().parent
SNAPSHOT_PATH = Path(os.environ.get("OFFLOAD_LOADING_SNAPSHOT_FILE", "team_loading_latest.json"))
if not SNAPSHOT_PATH.is_absolute():
    SNAPSHOT_PATH = ROOT / SNAPSHOT_PATH
ACTIVITY_WORKBOOK_PATH = Path(os.environ.get("CFE_ACTIVITY_WORKBOOK_FILE", str(ROOT / "CFE work overview.xlsx")))
if not ACTIVITY_WORKBOOK_PATH.is_absolute():
    ACTIVITY_WORKBOOK_PATH = ROOT / ACTIVITY_WORKBOOK_PATH
CURRENT_YEAR = datetime.now().year
HISTORY_PATH = Path(os.environ.get("OFFLOAD_LOADING_HISTORY_DIR", str(ROOT / "loading_history")))
if not HISTORY_PATH.is_absolute():
    HISTORY_PATH = ROOT / HISTORY_PATH
WEIGHT_MAP_PATH = ROOT / "issue_category_weights.json"
BATCH_PATH = ROOT / "run_offload_loading_summary_daily.bat"
RUN_LOCK = threading.Lock()
RUN_PROCESS: subprocess.Popen[bytes] | None = None
RUN_STARTED_AT: str | None = None
RUN_FINISHED_AT: str | None = None
RUN_RETURN_CODE: int | None = None
FIELD_ISSUE_TABLE = "ips_jira_bugs"
FIELD_ISSUE_KEYWORD = "field issue"
FIELD_ISSUE_DETAIL_COLUMNS = (
    "ips_case_number",
    "ips_title",
    "ips_status",
    "ips_url",
    "jira_id",
    "jira_summary",
    "jira_status",
    "jira_url",
    "customer",
)


def _workbook_value(value: object) -> object:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return value


def _is_current_year(value: object) -> bool:
    return bool(re.search(rf"(?<!\d){CURRENT_YEAR}(?!\d)", str(value or "")))


def _activity_category(sheet_name: str, headers: tuple[object, ...]) -> str:
    name = sheet_name.casefold()
    if any(term in name for term in ("budget", "shopping", "expense")):
        return "Budget & purchasing"
    if any(term in name for term in ("training", "skill", "evaluation")):
        return "Learning & skills"
    if (
        any(term in name for term in ("meeting", "rotation", "schedule", "presentation"))
        or (headers and str(headers[0]).casefold() == "ww/time")
    ):
        return "Schedules & events"
    if any(term in name for term in ("task", "project", "feature", "rnr")):
        return "Projects & tasks"
    return "Team reference"


def _load_activity_workbook() -> dict:
    if not ACTIVITY_WORKBOOK_PATH.is_file():
        raise FileNotFoundError(ACTIVITY_WORKBOOK_PATH.name)

    workbook = load_workbook(ACTIVITY_WORKBOOK_PATH, data_only=True, read_only=True)
    sheets = []
    try:
        for worksheet in workbook.worksheets:
            sheet_years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", worksheet.title)
            if sheet_years and CURRENT_YEAR not in {int(year) for year in sheet_years}:
                continue

            source_rows = list(worksheet.iter_rows(values_only=True))
            if not source_rows:
                continue
            header_row = tuple(source_rows[0])
            year_column = next(
                (index for index, value in enumerate(header_row) if str(value or "").strip().casefold() == "year"),
                None,
            )
            selected_columns = list(range(len(header_row)))
            if worksheet.title.casefold() == "cirm host rotation":
                selected_columns = [
                    index for index, value in enumerate(header_row)
                    if index == 0 or _is_current_year(value)
                ]

            rows = []
            for row_number, source_row in enumerate(source_rows, start=1):
                if row_number > 1 and year_column is not None:
                    if year_column >= len(source_row) or not _is_current_year(source_row[year_column]):
                        continue
                values = [
                    _workbook_value(source_row[index]) if index < len(source_row) else None
                    for index in selected_columns
                ]
                if any(value is not None for value in values):
                    rows.append({"row": row_number, "values": values})

            active_columns = [
                position for position in range(len(selected_columns))
                if any(row["values"][position] is not None for row in rows)
            ]
            selected_columns = [selected_columns[position] for position in active_columns]
            rows = [
                {"row": row["row"], "values": [row["values"][position] for position in active_columns]}
                for row in rows
            ]
            headers = tuple(header_row[index] if index < len(header_row) else None for index in selected_columns)
            sheets.append(
                {
                    "name": worksheet.title,
                    "category": _activity_category(worksheet.title, headers),
                    "columns": [
                        {
                            "letter": get_column_letter(index + 1),
                            "label": str(value).strip() if value is not None else get_column_letter(index + 1),
                        }
                        for index, value in zip(selected_columns, headers)
                    ],
                    "rows": rows,
                    "row_count": max(0, len(rows) - 1),
                }
            )
    finally:
        workbook.close()

    stat = ACTIVITY_WORKBOOK_PATH.stat()
    return {
        "available": True,
        "year": CURRENT_YEAR,
        "source": ACTIVITY_WORKBOOK_PATH.name,
        "source_updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
        "sheets": sheets,
    }


def _field_issue_text(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.upper() == "NA" else text


def _field_issue_active_expr(available: set[str]) -> str:
    def status(column: str) -> str:
        if column not in available:
            return "NULL"
        return f"NULLIF(NULLIF(UPPER(TRIM({column}::text)), ''), 'NA')"

    bug, ips, jira = status("bug_status"), status("ips_status"), status("jira_status")
    # bug_status wins; otherwise closed if any present IPS/Jira status is closed, and no status at all is not active.
    return (
        f"(CASE WHEN {bug} IS NOT NULL THEN {bug} <> 'CLOSED' "
        f"WHEN {ips} IS NULL AND {jira} IS NULL THEN FALSE "
        f"ELSE COALESCE({ips}, '') <> 'CLOSED' AND COALESCE({jira}, '') <> 'CLOSED' END)"
    )


def _load_field_issues() -> dict:
    db = Sherlock.PostgresCustomerEngineeringDb
    connection = psycopg2.connect(
        dbname=db.database, user=db.user, password=db.password, host=db.host, port=db.port, connect_timeout=10
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND table_schema = ANY(current_schemas(false))",
                (FIELD_ISSUE_TABLE,),
            )
            available = {row[0] for row in cursor.fetchall()}
            columns = ["engineer", *(name for name in FIELD_ISSUE_DETAIL_COLUMNS if name in available)]
            pattern = f"%{FIELD_ISSUE_KEYWORD}%"
            # bug_created_date mirrors vw_issues, which maps it to ips_created_date.
            cursor.execute(
                sql.SQL(
                    "SELECT {columns}, {active} AS is_active, ips_created_date AS bug_created_date FROM {table} "
                    "WHERE ips_title::text ILIKE %s OR jira_summary::text ILIKE %s"
                ).format(
                    columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
                    active=sql.SQL(_field_issue_active_expr(available)),
                    table=sql.Identifier(FIELD_ISSUE_TABLE),
                ),
                (pattern, pattern),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    issues = []
    for *values, is_active, created in rows:
        issue = {name: _field_issue_text(value) for name, value in zip(columns, values)}
        issue["engineer"] = issue["engineer"] or "Unassigned"
        issue["active"] = bool(is_active)
        issue["bug_created_date"] = created.date().isoformat() if created else ""
        issue["in_year"] = bool(created) and created.year == CURRENT_YEAR
        issues.append(issue)
    return {
        "keyword": FIELD_ISSUE_KEYWORD,
        "year": CURRENT_YEAR,
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
        "issues": issues,
    }


class LoadingDashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        requested_path = Path(self.translate_path(self.path)).resolve()
        # Never serve dotfiles such as .env (DB credentials) or .venv.
        if requested_path == ACTIVITY_WORKBOOK_PATH.resolve() or any(
            part.startswith(".") for part in unquote(parsed.path).split("/")
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/activity":
            self._send_activity()
            return
        if parsed.path == "/api/field-issues":
            self._send_field_issues()
            return
        if parsed.path == "/api/loading":
            self._send_snapshot(parse_qs(parsed.query).get("date", ["latest"])[0])
            return
        if parsed.path == "/api/dates":
            self._send_dates()
            return
        if parsed.path == "/api/weights":
            self._send_weights()
            return
        if parsed.path == "/api/batch":
            self._send_batch_status()
            return
        if parsed.path in ("", "/"):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run-batch":
            self._start_batch()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._add_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _add_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._add_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_snapshot(self, selected_date: str) -> dict | None:
        path = SNAPSHOT_PATH if selected_date in ("", "latest") else HISTORY_PATH / f"{selected_date}.json"
        try:
            with path.open("r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
            return snapshot if isinstance(snapshot, dict) else None
        except (OSError, ValueError):
            if selected_date not in ("", "latest"):
                latest = self._read_snapshot("latest")
                if latest and str(latest.get("generated_at", ""))[:10] == selected_date:
                    return latest
            return None

    def _send_dates(self) -> None:
        dates = []
        if HISTORY_PATH.exists():
            dates = sorted(
                (path.stem for path in HISTORY_PATH.glob("????-??-??.json")),
                reverse=True,
            )
        latest = self._read_snapshot("latest")
        latest_date = str(latest.get("generated_at", ""))[:10] if latest else ""
        if latest_date and latest_date not in dates:
            dates.insert(0, latest_date)
        self._send_json({"dates": dates})

    def _send_weights(self) -> None:
        payload = {"default_weight": 1.0, "category_weights": {}, "category_technology_weights": {}}
        try:
            with WEIGHT_MAP_PATH.open("r", encoding="utf-8") as handle:
                source = json.load(handle)
            if isinstance(source, dict):
                payload.update(
                    {
                        "default_weight": source.get("default_weight", 1.0),
                        "category_weights": source.get("category_weights") or {},
                        "category_technology_weights": source.get("category_technology_weights") or {},
                    }
                )
        except (OSError, ValueError):
            payload["message"] = "Weight configuration is unavailable."
        self._send_json(payload)

    def _send_snapshot(self, selected_date: str) -> None:
        payload = {
            "generated_at": None,
            "categories": [],
            "rows": [],
            "available": False,
            "selected_date": selected_date,
            "message": "No batch result is available yet. Run the daily loading batch first.",
        }
        snapshot = self._read_snapshot(selected_date)
        if snapshot is not None:
            payload.update(
                {
                    "generated_at": snapshot.get("generated_at"),
                    "categories": snapshot.get("categories") or [],
                    "rows": snapshot.get("rows") or [],
                    "available": True,
                    "message": "",
                }
            )

        self._send_json(payload)

    def _send_activity(self) -> None:
        try:
            self._send_json(_load_activity_workbook())
        except FileNotFoundError:
            self._send_json(
                {"available": False, "message": f"Workbook not found: {ACTIVITY_WORKBOOK_PATH.name}"},
                HTTPStatus.NOT_FOUND,
            )
        except Exception as exc:
            self._send_json(
                {"available": False, "message": f"Unable to read workbook ({type(exc).__name__})."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _send_field_issues(self) -> None:
        try:
            self._send_json(_load_field_issues())
        except Exception as exc:
            self._send_json(
                {"issues": [], "message": f"Unable to query {FIELD_ISSUE_TABLE} ({type(exc).__name__})."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _send_batch_status(self) -> None:
        global RUN_FINISHED_AT, RUN_RETURN_CODE
        with RUN_LOCK:
            process = RUN_PROCESS
            if process is not None and process.poll() is not None:
                RUN_RETURN_CODE = process.returncode
                if RUN_FINISHED_AT is None:
                    RUN_FINISHED_AT = datetime.now().isoformat(timespec="seconds")
            running = process is not None and process.poll() is None
            payload = {
                "running": running,
                "started_at": RUN_STARTED_AT,
                "finished_at": RUN_FINISHED_AT,
                "return_code": RUN_RETURN_CODE,
            }
        self._send_json(payload)

    def _start_batch(self) -> None:
        global RUN_PROCESS, RUN_STARTED_AT, RUN_FINISHED_AT, RUN_RETURN_CODE
        with RUN_LOCK:
            if RUN_PROCESS is not None and RUN_PROCESS.poll() is None:
                self._send_json({"running": True, "message": "The batch is already running."}, HTTPStatus.CONFLICT)
                return
            if not BATCH_PATH.exists():
                self._send_json({"running": False, "message": f"Batch file not found: {BATCH_PATH.name}"}, HTTPStatus.NOT_FOUND)
                return
            if os.name == "nt":
                command = ["cmd.exe", "/d", "/c", f"{BATCH_PATH} --no-email"]
            else:
                command = ["sh", str(BATCH_PATH), "--no-email"]
            RUN_PROCESS = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            RUN_STARTED_AT = datetime.now().isoformat(timespec="seconds")
            RUN_FINISHED_AT = None
            RUN_RETURN_CODE = None
        self._send_json({"running": True, "started_at": RUN_STARTED_AT}, HTTPStatus.ACCEPTED)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the weighted team loading dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LoadingDashboardHandler)
    print(f"Loading dashboard: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
