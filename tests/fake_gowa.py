"""Fake GoWA: a real HTTP server on 127.0.0.1, random port.

Mimics only the routes `wa` uses, requires Basic Auth and keeps every request in
`calls` for the tests to check. Nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SECRET = "test-secret"
USER = "wa-mac"


class FakeGowa:
    def __init__(self, logged_in: bool = True, connected: bool = True, device: bool = True):
        self.logged_in = logged_in
        self.connected = connected
        self.device = device
        self.jid = "5561988887777@s.whatsapp.net" if logged_in else ""
        self.pair = {"state": "idle"}
        self.calls: list[dict] = []
        self.inbox_messages: list[dict] = []
        self.fail: dict[str, tuple[int, dict]] = {}  # caminho -> (status, corpo)
        self.pair_after_polls: int | None = None  # pairs after N status checks
        self.fail_pair_after_polls: int | None = None
        self._polls = 0
        self._counter = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silencioso
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                call = {"method": method, "path": path, "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                        "body": body, "device": self.headers.get("X-Device-Id"),
                        "auth": self.headers.get("Authorization")}
                fake.calls.append(call)
                if path == "/health":
                    return self._send(200, "OK")
                expected = "Basic " + base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
                if call["auth"] != expected:
                    return self._send(401, {"code": "UNAUTHORIZED", "message": "Unauthorized"})
                if path in fake.fail:
                    status, payload = fake.fail[path]
                    return self._send(status, payload)
                return fake.route(self, method, path, call)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    # ------------------------------------------------------------ rotas

    def ok(self, h, results, message="Success"):
        h._send(200, {"code": "SUCCESS", "message": message, "results": results})

    def route(self, h, method, path, call):
        if path == "/devices/wa-mac" and method == "GET":
            if not self.device:
                # the real GoWA answers 500 with this message
                return h._send(500, {"code": "INTERNAL_SERVER_ERROR", "message": "device wa-mac not found"})
            return self.ok(h, {"id": "wa-mac", "jid": self.jid})
        if path == "/devices" and method == "POST":
            self.device = True
            return self.ok(h, {"device_id": (call["body"] or {}).get("device_id")})
        if path == "/devices" and method == "GET":
            return self.ok(h, [])
        if path == "/devices/wa-mac/login/code" and method == "POST":
            self.pair = {"state": "code_issued"}
            return self.ok(h, {"device_id": "wa-mac", "pair_code": "ABCD1234"})
        if path == "/devices/wa-mac/logout" and method == "POST":
            self.logged_in = False
            self.jid = ""
            return self.ok(h, None, "Device logged out")
        if path == "/wa-mac/pair-status":
            self._polls += 1
            if self.pair_after_polls is not None and self._polls >= self.pair_after_polls:
                self.logged_in = True
                self.connected = True
                self.jid = "5561988887777@s.whatsapp.net"
                self.pair = {"state": "paired"}
            if self.fail_pair_after_polls is not None and self._polls >= self.fail_pair_after_polls:
                self.pair = {"state": "failed", "error": "bad code"}
            return self.ok(h, {"device_id": "wa-mac", "jid": self.jid, "is_connected": self.connected,
                               "is_logged_in": self.logged_in, "pair": self.pair})
        if path == "/send/message" and method == "POST":
            self._counter += 1
            return self.ok(h, {"message_id": f"3EB0FAKE{self._counter:04d}", "status": "Message sent"})
        if path.startswith("/chat/") and method == "POST":
            return self.ok(h, {"chat_jid": path.split("/")[2], **(call["body"] or {})})
        if path == "/label" and method == "POST":
            return self.ok(h, call["body"])
        if path == "/wa-mac/inbox":
            return self.ok(h, {"since": call["query"].get("since"), "count": len(self.inbox_messages),
                               "messages": self.inbox_messages})
        return h._send(404, {"code": "NOT_FOUND", "message": path})

    # ------------------------------------------------------------ util

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def posts(self, prefix=""):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].startswith(prefix)]
