"""HTTP transport for the vector-clock audit service (stdlib only).

Endpoints:
    GET  /healthz             liveness/readiness probe
    POST /api/events/audit    validate a batch and return its causal plan
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import AuditError, audit_batch

MAX_BODY_BYTES = 1 << 20          # 1 MiB request-body limit
DRAIN_CAP_BYTES = 2 << 20         # drain oversize bodies up to this before 413

AUDIT_PATH = "/api/events/audit"


def _error_payload(code, message, event_ids=(), details=None):
    return {
        "error": {
            "code": code,
            "message": message,
            "event_ids": sorted({str(e) for e in event_ids}),
            "details": details or {},
        }
    }


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "VectorClockAudit/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------

    def _send_json(self, status, payload, close=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        from urllib.parse import urlsplit
        return urlsplit(self.path).path

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        path = self._path()
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif path == "/":
            self._send_json(200, {
                "service": "vector-clock-audit",
                "endpoints": {
                    "GET /healthz": "liveness probe",
                    "POST /api/events/audit": "validate an event batch and "
                                              "return its causal layers",
                },
            })
        elif path == AUDIT_PATH:
            self._send_json(405, _error_payload(
                "METHOD_NOT_ALLOWED", "use POST for %s" % AUDIT_PATH))
        else:
            self._send_json(404, _error_payload(
                "NOT_FOUND", "unknown path: %s" % path))

    def do_POST(self):
        path = self._path()
        if path != AUDIT_PATH:
            if path in ("/healthz", "/"):
                self._send_json(405, _error_payload(
                    "METHOD_NOT_ALLOWED", "use GET for %s" % path))
            else:
                self._send_json(404, _error_payload(
                    "NOT_FOUND", "unknown path: %s" % path))
            return

        length = self.headers.get("Content-Length")
        if length is None:
            self._send_json(411, _error_payload(
                "LENGTH_REQUIRED", "Content-Length header is required"))
            return
        try:
            size = int(length)
        except ValueError:
            self._send_json(400, _error_payload(
                "MALFORMED_JSON", "invalid Content-Length header"))
            return
        if size < 0:
            self._send_json(400, _error_payload(
                "MALFORMED_JSON", "invalid Content-Length header"))
            return
        if size > MAX_BODY_BYTES:
            if size <= DRAIN_CAP_BYTES:
                self._drain(size)
                self._send_json(413, _error_payload(
                    "PAYLOAD_TOO_LARGE",
                    "request body must not exceed %d bytes (1 MiB)"
                    % MAX_BODY_BYTES))
            else:
                self._send_json(413, _error_payload(
                    "PAYLOAD_TOO_LARGE",
                    "request body must not exceed %d bytes (1 MiB)"
                    % MAX_BODY_BYTES), close=True)
            return

        body = self.rfile.read(size)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, _error_payload(
                "MALFORMED_JSON", "request body is not valid JSON"))
            return

        try:
            result = audit_batch(payload)
        except AuditError as err:
            self._send_json(err.status, err.to_payload())
            return
        except Exception:  # pragma: no cover - defensive
            import traceback
            traceback.print_exc()
            self._send_json(500, _error_payload(
                "INTERNAL_ERROR", "unexpected server error"))
            return
        self._send_json(200, result)

    def do_PUT(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        self._send_json(405, _error_payload(
            "METHOD_NOT_ALLOWED", "method not allowed for %s" % self._path()))

    def _drain(self, size):
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def log_message(self, fmt, *args):  # keep container logs useful
        import sys
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), AuditHandler)
    server.daemon_threads = True
    print("vector-clock-audit listening on %s:%d" % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
