"""Fake GoWA: a real HTTP server on 127.0.0.1, random port.

Mimics only the routes `wa` uses, requires Basic Auth and keeps every request in
`calls` for the tests to check. Nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
import threading
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SECRET = "test-secret"
USER = "wa-mac"


def parse_multipart(ctype: str, raw: bytes) -> tuple[dict, dict | None]:
    """Form fields and the uploaded file of a multipart/form-data body."""
    msg = BytesParser(policy=email_policy.HTTP).parsebytes(
        f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
    form: dict = {}
    upload = None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename is not None:
            upload = {"field": name, "filename": filename, "content_type": part.get_content_type(), "data": data}
        else:
            form[name] = data.decode()
    return form, upload


class FakeGowa:
    def __init__(self, logged_in: bool = True, connected: bool = True, device: bool = True):
        self.logged_in = logged_in
        self.connected = connected
        self.device = device
        self.jid = "5561988887777@s.whatsapp.net" if logged_in else ""
        self.pair = {"state": "idle"}
        self.calls: list[dict] = []
        self.inbox_messages: list[dict] = []
        # Lists as GoWA keeps them from the app-state events (label id -> list).
        self.lists: dict[str, dict] = {}
        self.list_echo = True  # False: GoWA accepts the patch but the echo never arrives
        self.lists_synced_at: str | None = "2026-11-26T12:00:00Z"
        self.fail: dict[str, tuple[int, dict]] = {}  # caminho -> (status, corpo)
        # Media: limits in /app/info, files GoWA can download (stanza id -> (chat jid, file name, bytes)).
        self.app_info = {"version": "test", "max_file_size": 50_000_000, "max_video_size": 100_000_000,
                         "max_image_size": 20_000_000}
        self.downloads: dict[str, tuple[str, str, bytes]] = {}
        self.media_root = None  # GoWA's working folder (WA_GOWA_DIR); downloads land in statics/media
        self.download_path_override: str | None = None
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
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type") or ""
                form = upload = None
                if ctype.startswith("multipart/form-data"):
                    body, form, upload = None, *parse_multipart(ctype, raw)
                else:
                    body = json.loads(raw or b"null") if length else None
                call = {"method": method, "path": path, "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                        "body": body, "form": form, "upload": upload, "device": self.headers.get("X-Device-Id"),
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
        if path == "/app/info":
            return self.ok(h, self.app_info)
        if path.startswith("/send/") and path.split("/")[2] in ("image", "video", "audio", "file", "sticker"):
            self._counter += 1
            return self.ok(h, {"message_id": f"3EB0MEDIA{self._counter:04d}", "status": "Media sent"})
        if path.startswith("/message/") and path.endswith("/download") and method == "GET":
            stanza = path.split("/")[2]
            entry = self.downloads.get(stanza)
            if not entry or entry[0] != call["query"].get("phone"):
                return h._send(500, {"code": "INTERNAL_SERVER_ERROR", "message": f"message not found: {stanza}"})
            chat, name, data = entry
            rel = f"statics/media/{chat.split('@')[0]}/2026-11-26/{name}"
            target = self.media_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return self.ok(h, {"message_id": stanza, "status": "Media downloaded successfully", "media_type": "image",
                               "filename": name, "file_path": self.download_path_override or rel,
                               "file_size": len(data)})
        if path == "/send/message" and method == "POST":
            self._counter += 1
            return self.ok(h, {"message_id": f"3EB0FAKE{self._counter:04d}", "status": "Message sent"})
        if path.startswith("/chat/") and path.endswith("/label") and method == "POST":
            body = call["body"] or {}
            jid = path.split("/")[2]
            if self.list_echo:
                item = self.lists.setdefault(str(body["label_id"]), self.new_list(str(body["label_id"]), ""))
                jids = set(item["jids"])
                (jids.add if body.get("labeled") else jids.discard)(jid)
                item["jids"] = sorted(jids)
            return self.ok(h, {"chat_jid": jid, **body})
        if path.startswith("/chat/") and method == "POST":
            return self.ok(h, {"chat_jid": path.split("/")[2], **(call["body"] or {})})
        if path == "/label" and method == "POST":
            body = call["body"] or {}
            if self.list_echo:
                item = self.lists.setdefault(str(body["label_id"]), self.new_list(str(body["label_id"]), ""))
                item.update(name=body.get("name") or item["name"], type=body.get("type") or "CUSTOM",
                            deleted=bool(body.get("deleted")))
            return self.ok(h, body)
        if path == "/wa-mac/lists" and method == "GET":
            items = sorted(self.lists.values(), key=lambda i: int(i["id"]))
            return self.ok(h, {"count": len(items), "lists": items, "synced_at": self.lists_synced_at})
        if path == "/wa-mac/inbox":
            return self.ok(h, {"since": call["query"].get("since"), "count": len(self.inbox_messages),
                               "messages": self.inbox_messages})
        return h._send(404, {"code": "NOT_FOUND", "message": path})

    # ------------------------------------------------------------ util

    @staticmethod
    def new_list(label_id: str, name: str, jids=(), deleted: bool = False) -> dict:
        return {"id": label_id, "name": name, "type": "CUSTOM", "color": 0, "order_index": 0, "active": True,
                "deleted": deleted, "jids": sorted(jids), "updated_at": "2026-11-26T12:00:00Z"}

    def add_list(self, label_id: str, name: str, jids=(), deleted: bool = False) -> None:
        self.lists[label_id] = self.new_list(label_id, name, jids, deleted)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def posts(self, prefix=""):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].startswith(prefix)]
