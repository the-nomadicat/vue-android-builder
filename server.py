"""
android-builder HTTP server.

Endpoints:
  GET  /health              -- liveness + SDK ready check
  GET  /projects            -- list discovered buildable projects
  GET  /status              -- current / last batch state
  POST /build               -- start a build batch
  GET  /logs/<project>      -- last 200 lines of most recent build log (plain text)
  GET  /logs/<project>/json -- structured log response
"""

import glob
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import build_manager as bm

PORT = int(os.environ.get("PORT", "8080"))


def _sdk_ready() -> bool:
    sdk = os.environ.get("ANDROID_HOME", "/mount/android-sdk")
    return os.path.isfile(os.path.join(sdk, "cmdline-tools/latest/bin/sdkmanager"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Simple timestamped access log
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stdout.write(f"[{ts}] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str):
        body = text.encode("utf-8", errors="replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _parse_path(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/"), parsed

    # ── GET ─────────────────────────────────────────────────────────────────
    def do_GET(self):
        path, _ = self._parse_path()

        # GET /health
        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "sdk_ready": _sdk_ready(),
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "projects_root": bm.PROJECTS_ROOT,
            })
            return

        # GET /projects
        if path == "/projects":
            projects = bm.discover_projects()
            self._send_json(200, {
                "projects": projects,
                "count": len(projects),
            })
            return

        # GET /status
        if path == "/status":
            self._send_json(200, bm.state.to_dict())
            return

        # GET /logs/<project>  or  /logs/<project>/json
        if path.startswith("/logs/"):
            rest = path[len("/logs/"):]
            parts = rest.split("/", 1)
            project_name = parts[0]
            fmt = parts[1] if len(parts) > 1 else "text"

            log_files = bm.get_log_files(project_name)
            if not log_files:
                self._send_json(404, {"error": f"No logs found for: {project_name}"})
                return

            log_path = log_files[-1]  # most recent

            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return

            if fmt == "json":
                self._send_json(200, {
                    "project": project_name,
                    "log_file": os.path.basename(log_path),
                    "total_lines": len(all_lines),
                    "lines": [l.rstrip() for l in all_lines[-200:]],
                    "all_logs": [os.path.basename(p) for p in log_files],
                })
            else:
                self._send_text(200, "".join(all_lines[-200:]))
            return

        self._send_json(404, {"error": "Not found", "path": path})

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        path, _ = self._parse_path()

        # POST /build  or  POST /build/<project>
        if path == "/build" or path.startswith("/build/"):
            # URL-based single-project shortcut: POST /build/<project>
            # Avoids SSH/shell quoting issues with JSON bodies entirely.
            url_project = path[len("/build/"):] if path.startswith("/build/") else None

            try:
                body = self._read_body()
                params = json.loads(body) if body else {}
            except Exception:
                params = {}

            if not _sdk_ready():
                self._send_json(503, {
                    "error": "Android SDK not ready yet — container may still be initialising. Check /health."
                })
                return

            # URL project takes priority; fall back to body params
            if url_project:
                project_names = [url_project]
            else:
                raw_projects = params.get("projects")
                if raw_projects == "all" or raw_projects is None:
                    project_names = None
                elif isinstance(raw_projects, str):
                    project_names = [raw_projects]
                elif isinstance(raw_projects, list):
                    project_names = raw_projects
                else:
                    self._send_json(400, {"error": "projects must be 'all', a string, or a list"})
                    return

            max_parallel = int(params.get("parallel", 3))
            if max_parallel < 1:
                max_parallel = 1
            if max_parallel > 6:
                max_parallel = 6

            ok, result = bm.start_batch(project_names=project_names, max_parallel=max_parallel)
            if ok:
                self._send_json(200, {
                    "started": True,
                    "job_id": result,
                    "message": "Build started. Poll GET /status every 30s for progress.",
                    "parallel": max_parallel,
                    "projects": project_names or "all",
                })
            else:
                self._send_json(409, {"error": result})
            return

        self._send_json(404, {"error": "Not found", "path": path})


def main():
    os.makedirs(bm.LOGS_DIR, exist_ok=True)
    print(f"[server] android-builder starting on port {PORT}")
    print(f"[server] PROJECTS_ROOT = {bm.PROJECTS_ROOT}")
    print(f"[server] LOGS_DIR      = {bm.LOGS_DIR}")
    print(f"[server] SDK ready     = {_sdk_ready()}")
    print(f"[server] TailDrive URL = {bm.TAILDRIVE_BASE}")
    sys.stdout.flush()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[server] Listening on 0.0.0.0:{PORT}")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
