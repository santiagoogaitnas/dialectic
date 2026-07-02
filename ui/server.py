#!/usr/bin/env python3
"""Dialectic web UI server.

Provides a dashboard for launching, monitoring, and stopping chains.
Uses http.server + threading for simplicity (no external deps required).

Usage:
    python3 -m ui.server              # start on port 8420
    python3 -m ui.server --port 9000  # custom port
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Add project root to path so we can import chain and registry
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import registry as reg
from ui import directory_browser, project_view

logger = logging.getLogger("ui.server")

STATIC_DIR = Path(__file__).parent / "static"
ROLES_DIR = PROJECT_ROOT / "roles"

# chain IDs are generated as MMDDHHmm-xxxx; reject anything outside that shape
# so path components can never escape WORKSPACE.
_CHAIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_chain_id(chain_id: str) -> bool:
    return bool(chain_id) and _CHAIN_ID_RE.match(chain_id) is not None


def _json_response(handler, data, status=200):
    """Send a JSON response."""
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _sse_response(handler):
    """Begin an SSE response (headers only)."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()


def _read_chain_log(chain_id: str, tail_lines: int = 100) -> str:
    """Read the tail of a chain's log file."""
    if not _safe_chain_id(chain_id):
        return ""
    log_path = reg.WORKSPACE / chain_id / "chain_log.md"
    if not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        if len(lines) > tail_lines:
            return "\n".join(lines[-tail_lines:])
        return text
    except OSError:
        return ""


def _read_bulletin(chain_id: str) -> str:
    """Read a chain's bulletin file."""
    if not _safe_chain_id(chain_id):
        return ""
    bulletin_path = reg.WORKSPACE / chain_id / "bulletin.md"
    if bulletin_path.exists():
        try:
            return bulletin_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return ""


def _list_roles() -> list[str]:
    """List available role files."""
    if not ROLES_DIR.exists():
        return []
    return sorted(f.name for f in ROLES_DIR.glob("*.txt"))


def _chain_record_to_dict(record) -> dict:
    """Convert a ChainRecord to a JSON-safe dict."""
    if hasattr(record, "to_dict"):
        d = record.to_dict()
    elif isinstance(record, dict):
        d = record
    else:
        d = vars(record)
    # Convert timestamps to readable strings
    for key in ("started_at", "stopped_at", "last_activity"):
        val = d.get(key)
        if isinstance(val, (int, float)) and val > 0:
            d[key + "_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(val))
    return d


def _launch_chain_background(seed, role_a, role_b, project, session, max_chains, focus=""):
    """Launch a chain in a background process.

    `focus` is a free-text label (e.g. "backend", "tests") forwarded to
    chain.py via --focus. chain.py's ChainCoordinatorContext writes it
    into project_coordinator on boot, so the UI does not need to register
    the chain itself.
    """
    cmd = [sys.executable, str(PROJECT_ROOT / "chain.py"),
           seed, "--role-a", role_a, "--role-b", role_b]
    if project:
        cmd.extend(["--project", project])
    if session:
        cmd.extend(["--session", session])
    if max_chains > 0:
        cmd.extend(["--max-chains", str(max_chains)])
    if focus and focus.strip():
        cmd.extend(["--focus", focus.strip()])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


class DialecticHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the Dialectic UI."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format, *args):
        logger.debug(format, *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API endpoints
        if path == "/api/chains":
            chains = reg.list_chains()
            data = [_chain_record_to_dict(c) for c in chains]
            _json_response(self, data)
            return

        if path.startswith("/api/chains/") and path.endswith("/log"):
            chain_id = path.split("/")[3]
            params = parse_qs(parsed.query)
            tail = int(params.get("tail", ["100"])[0])
            log_text = _read_chain_log(chain_id, tail)
            _json_response(self, {"chain_id": chain_id, "log": log_text})
            return

        if path.startswith("/api/chains/") and path.endswith("/bulletin"):
            chain_id = path.split("/")[3]
            text = _read_bulletin(chain_id)
            _json_response(self, {"chain_id": chain_id, "bulletin": text})
            return

        if path.startswith("/api/chains/") and path.endswith("/plan"):
            chain_id = path.split("/")[3]
            if not _safe_chain_id(chain_id):
                _json_response(self, {"error": "bad chain_id"}, 400)
                return
            if reg.get_chain(chain_id) is None:
                _json_response(self, {"error": "not found"}, 404)
                return
            plan = reg.read_plan_text(chain_id)
            _json_response(self, {"chain_id": chain_id, "plan": plan or ""})
            return

        if path.startswith("/api/chains/") and path.endswith("/events"):
            chain_id = path.split("/")[3]
            self._handle_sse(chain_id)
            return

        if path.startswith("/api/chains/") and path.endswith("/detail"):
            chain_id = path.split("/")[3]
            if not _safe_chain_id(chain_id):
                _json_response(self, {"error": "bad chain_id"}, 400)
                return
            record = reg.get_chain(chain_id)
            if record is None:
                _json_response(self, {"error": "not found"}, 404)
                return
            params = parse_qs(parsed.query)
            tail = int(params.get("tail", ["200"])[0])
            detail = {
                "record": _chain_record_to_dict(record),
                "log": _read_chain_log(chain_id, tail),
                "bulletin": _read_bulletin(chain_id),
                "plan": reg.read_plan_text(chain_id) or "",
            }
            _json_response(self, detail)
            return

        if path.startswith("/api/chains/") and not path.endswith("/"):
            chain_id = path.split("/")[3]
            record = reg.get_chain(chain_id)
            if record:
                _json_response(self, _chain_record_to_dict(record))
            else:
                _json_response(self, {"error": "not found"}, 404)
            return

        if path == "/api/events":
            self._handle_list_sse()
            return

        if path == "/api/roles":
            _json_response(self, _list_roles())
            return

        if path == "/api/projects":
            _json_response(self, project_view.list_projects_with_chains())
            return

        if path == "/api/projects/detail":
            params = parse_qs(parsed.query)
            project_path = (params.get("path", [""])[0] or "").strip()
            if not project_path:
                _json_response(self, {"error": "path is required"}, 400)
                return
            detail = project_view.get_project_detail(project_path)
            detail["summary"] = project_view.project_summary_text(project_path)
            _json_response(self, detail)
            return

        if path == "/api/projects/conflicts":
            params = parse_qs(parsed.query)
            project_path = (params.get("path", [""])[0] or "").strip()
            if not project_path:
                _json_response(self, {"error": "path is required"}, 400)
                return
            conflicts = project_view.list_project_conflicts(project_path)
            _json_response(
                self,
                {"project": project_path, "conflicts": conflicts},
            )
            return

        if path == "/api/browse":
            params = parse_qs(parsed.query)
            browse_path = (
                params.get("path", [""])[0]
                or params.get("start", [""])[0]
                or str(Path.home())
            )
            include_hidden = params.get("include_hidden", ["0"])[0] in ("1", "true")
            include_files = params.get("include_files", ["0"])[0] in ("1", "true")
            try:
                max_entries = int(params.get("max_entries", ["500"])[0])
            except ValueError:
                max_entries = 500
            result = directory_browser.browse(
                browse_path,
                include_hidden=include_hidden,
                include_files=include_files,
                max_entries=max_entries,
            )
            result["suggestions"] = directory_browser.suggestions()
            _json_response(self, result)
            return

        if path == "/api/validate_project":
            params = parse_qs(parsed.query)
            target = (params.get("path", [""])[0] or "").strip()
            ok, reason = directory_browser.validate_project_dir(target)
            _json_response(self, {"ok": ok, "reason": reason, "path": target})
            return

        # Static files
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def _handle_sse(self, chain_id):
        """Stream chain status updates via Server-Sent Events.

        Sends status frames every ~2s. After the chain ends (stopped/dead) we
        send one final frame and close. If the client disconnects we notice on
        the next write and exit quietly. Also emits a comment heartbeat every
        loop so idle proxies don't time the connection out.
        """
        if not _safe_chain_id(chain_id):
            _json_response(self, {"error": "bad chain_id"}, 400)
            return
        _sse_response(self)
        last_log_size = 0
        try:
            for _ in range(300):  # max ~10 min at 2s interval
                record = reg.get_chain(chain_id)
                if not record:
                    self.wfile.write(f"data: {json.dumps({'type': 'gone'})}\n\n".encode())
                    self.wfile.flush()
                    return

                d = _chain_record_to_dict(record)
                d["type"] = "status"

                log_path = reg.WORKSPACE / chain_id / "chain_log.md"
                if log_path.exists():
                    size = log_path.stat().st_size
                    if size != last_log_size:
                        d["log_updated"] = True
                        last_log_size = size

                self.wfile.write(f"data: {json.dumps(d)}\n\n".encode())
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()

                if record.status in ("stopped", "dead"):
                    return
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _handle_list_sse(self):
        """Stream the full chain list as SSE, only emitting when it changes.

        The dashboard subscribes to this instead of polling every few seconds.
        We snapshot registry state each tick and send a frame only when any
        chain has a different status, round, last_activity, or the set of
        chain_ids changed. A heartbeat comment keeps the connection alive
        through idle periods.
        """
        _sse_response(self)
        last_snapshot = None
        try:
            for _ in range(600):  # max ~20 min at 2s interval
                chains = [_chain_record_to_dict(c) for c in reg.list_chains()]
                snap_key = tuple(
                    (c.get("chain_id"), c.get("status"), c.get("current_round"),
                     c.get("last_activity"))
                    for c in chains
                )
                if snap_key != last_snapshot:
                    payload = {"type": "chains", "chains": chains}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                    self.wfile.flush()
                    last_snapshot = snap_key
                # heartbeat comment — proxies/browsers time out idle streams
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/chains":
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}

            seed = body.get("seed", "").strip()
            if not seed:
                _json_response(self, {"error": "seed is required"}, 400)
                return

            role_a = body.get("role_a", "builder.txt")
            role_b = body.get("role_b", "thinker.txt")
            project = body.get("project", None)
            session = body.get("session", None)
            max_chains = body.get("max_chains", 5)
            focus = (body.get("focus") or "").strip()

            if project:
                ok, reason = directory_browser.validate_project_dir(project)
                if not ok:
                    _json_response(self, {"error": reason}, 400)
                    return

            try:
                pid = _launch_chain_background(
                    seed, role_a, role_b, project, session, max_chains,
                    focus=focus,
                )
                _json_response(self, {
                    "status": "launched",
                    "pid": pid,
                    "focus_applied": bool(focus and project),
                }, 201)
            except Exception as e:
                _json_response(self, {"error": str(e)}, 500)
            return

        _json_response(self, {"error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/chains/"):
            chain_id = path.split("/")[3]
            if reg.stop_chain(chain_id):
                _json_response(self, {"status": "stopped", "chain_id": chain_id})
            else:
                _json_response(self, {"error": "not found"}, 404)
            return

        _json_response(self, {"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def run_server(port: int = 8420, host: str = "0.0.0.0"):
    # ThreadingHTTPServer is required: SSE connections hold a socket for up to
    # 10 minutes each, and the stdlib HTTPServer is single-threaded, so a
    # single open log viewer would freeze every other API request.
    server = ThreadingHTTPServer((host, port), DialecticHandler)
    server.daemon_threads = True
    logger.info(f"Dialectic UI running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Dialectic web UI")
    parser.add_argument("--port", type=int, default=8420, help="Port (default: 8420)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    args = parser.parse_args()
    run_server(args.port, args.host)
