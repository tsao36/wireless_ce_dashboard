"""Serve the latest weighted team loading snapshot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
SNAPSHOT_PATH = Path(os.environ.get("OFFLOAD_LOADING_SNAPSHOT_FILE", "team_loading_latest.json"))
if not SNAPSHOT_PATH.is_absolute():
    SNAPSHOT_PATH = ROOT / SNAPSHOT_PATH
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


class LoadingDashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
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
    parser.add_argument("--host", default="0.0.0.0")
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
