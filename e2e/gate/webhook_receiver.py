# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Local alert-webhook receiver for the release gate (standard library only).

Runs in its own container on the gate's Docker network. Modus delivers alert
notifications to it; the gate reads what arrived and switches its behaviour to
make the connection look healthy, slow or failing.

    GET  /_gate/health     200
    GET  /_gate/received   JSON list of every POST received on other paths
    POST /_gate/mode       {"mode": "ok" | "slow" | "fail"}
    *    anything else     answered according to the current mode
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"mode": "ok", "received": []}
LOCK = threading.Lock()
SLOW_SECONDS = 2.2   # above the 1.5 s "degraded" latency threshold


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; the gate reads /_gate/received
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _by_mode(self):
        with LOCK:
            mode = STATE["mode"]
        if mode == "slow":
            time.sleep(SLOW_SECONDS)
        if mode == "fail":
            self._send(503, {"error": "receiver failing (gate chaos)"})
        else:
            self._send(200, {"ok": True})

    def do_GET(self):
        if self.path == "/_gate/health":
            return self._send(200, {"ok": True})
        if self.path == "/_gate/received":
            with LOCK:
                return self._send(200, list(STATE["received"]))
        return self._by_mode()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        raw = self._body()
        if self.path == "/_gate/mode":
            mode = json.loads(raw or b"{}").get("mode", "ok")
            with LOCK:
                STATE["mode"] = mode
            return self._send(200, {"mode": mode})
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        with LOCK:
            mode = STATE["mode"]
            STATE["received"].append({"path": self.path, "at": time.time(), "mode": mode,
                                      "payload": payload})
        return self._by_mode()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
