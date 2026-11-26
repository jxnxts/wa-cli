"""Fake WhatsApp for the tests: simulates `open`, `osascript` (menu bar) and the
app writing the new state to the synthetic database. Nothing real is run.

The menus use the Brazilian Portuguese titles observed on a real account; with
`lang="en"` they use the English titles from the app's localization files."""

from __future__ import annotations

import sqlite3
import subprocess
from urllib.parse import parse_qs, urlparse

from conftest import APPLE, NOW

L = "‎"

# pt-BR title -> English title (WhatsApp localization files).
EN = {
    "Arquivo": "File", "Conversa": "Chat", "Ligar": "Call",
    "Ligação de vídeo": "Video call", "Ligação de voz": "Voice call",
    "Marcar como lida": "Mark as read", "Marcar como não lida": "Mark as unread",
    "Arquivar": "Archive", "Desarquivar": "Unarchive", "Fixar": "Pin", "Desafixar": "Unpin",
    "Silenciar": "Mute", "Reativar som": "Unmute sound",
    "8 horas": "8 hours", "1 semana": "1 week", "Sempre": "Always",
    "Bloquear…": "Block…", "Apagar conversa…": "Delete chat…",
    "Silenciar microfone": "Mute microphone", "Encerrar ligação": "End Call",
    "Câmera": "Camera", "Microfone": "Microphone", "Alto-falante": "Speaker",
}
PT = {v: k for k, v in EN.items()}


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def _item(title, enabled=True, sub=None):
    return {"title": L + title if title else None, "enabled": enabled, "sub": sub}


class FakeWhatsApp:
    def __init__(self, db_path, chat_by_phone: dict[str, int], *, front="WhatsApp", write_db=True,
                 call_starts=True, hangup_works=True, devices_mic=None, devices_out=None, stderr_for=None,
                 ignore_clicks=0, lang="pt"):
        self.lang = lang
        self.db_path = db_path
        self.chat_by_phone = chat_by_phone
        self.front = front
        self.write_db = write_db
        self.call_starts = call_starts
        self.hangup_works = hangup_works
        self.stderr_for = stderr_for or {}
        self.ignore_clicks = ignore_clicks  # clicks that "do not take" (nothing changes)
        self.calls: list[list[str]] = []
        self.clicked: list[str] = []
        self.current_chat: int | None = None
        self.in_call = False
        self.mic = None
        self.out = None
        self.devices_mic = devices_mic or ["MacBook Pro Microphone", "BlackHole 16ch"]
        self.devices_out = devices_out or ["MacBook Pro Speakers", "BlackHole 2ch"]
        self.state = {}  # pk -> dict(archived, pinned, muted, unread)

    # ---------------------------------------------------------- state

    def _st(self):
        pk = self.current_chat
        if pk not in self.state:
            c = sqlite3.connect(self.db_path)
            row = c.execute("SELECT ZARCHIVED, ZUNREADCOUNT, ZLASTMESSAGEDATE FROM ZWACHATSESSION WHERE Z_PK=?", (pk,)).fetchone()
            c.close()
            self.state[pk] = {
                "archived": bool(row[0]) if row else False,
                "unread": bool(row[1]) if row else False,
                "pinned": bool(row and row[2] and row[2] > NOW + 86400 * 365),
                "muted": False,
            }
        return self.state[pk]

    def _db(self, sql, params=()):
        if not self.write_db or self.current_chat is None:
            return
        c = sqlite3.connect(self.db_path)
        c.execute(sql, params)
        c.commit()
        c.close()

    # ---------------------------------------------------------- menus

    def _t(self, title):
        return EN.get(title, title) if self.lang == "en" and title else title

    def _i(self, title, enabled=True, sub=None):
        return _item(self._t(title), enabled, sub)

    def menus(self):
        _item = self._i  # noqa: N806 (titles follow self.lang)
        bar = [("Apple", None), (L + "WhatsApp", []), (L + self._t("Arquivo"), [])]
        if self.current_chat is not None:
            st = self._st()
            conv = [
                _item("Ligação de vídeo"),
                _item("Ligação de voz", not self.in_call),
                _item(None, False),
                _item("Marcar como lida" if st["unread"] else "Marcar como não lida"),
                _item("Desarquivar" if st["archived"] else "Arquivar"),
                _item("Desafixar" if st["pinned"] else "Fixar"),
                _item("Reativar som") if st["muted"] else _item("Silenciar", sub=[_item("8 horas"), _item("1 semana"), _item("Sempre")]),
                _item("Bloquear…"),
                _item("Apagar conversa…"),
            ]
            bar.append((L + self._t("Conversa"), conv))
        if self.in_call:
            bar.append((L + self._t("Ligar"), [
                _item("Silenciar microfone"),
                _item("Encerrar ligação"),
                _item("Câmera", sub=[_item("FaceTime HD")]),
                _item("Microfone", sub=[_item(d) for d in self.devices_mic]),
                _item("Alto-falante", sub=[_item(d) for d in self.devices_out]),
            ]))
        return bar

    def _resolve(self, path):
        bar = self.menus()
        title, items = bar[path[0] - 1]
        node = {"title": title, "sub": items}
        for idx in path[1:]:
            node = node["sub"][idx - 1]
        return node

    def _click(self, node):
        t = node["title"].replace(L, "")
        self.clicked.append(t)
        t = PT.get(t, t)  # the logic below works on the pt-BR titles
        if self.ignore_clicks > 0:
            self.ignore_clicks -= 1
            return
        st = self._st() if self.current_chat is not None else None
        pk = self.current_chat
        if t == "Arquivar":
            st["archived"] = True
            self._db("UPDATE ZWACHATSESSION SET ZARCHIVED=1 WHERE Z_PK=?", (pk,))
        elif t == "Desarquivar":
            st["archived"] = False
            self._db("UPDATE ZWACHATSESSION SET ZARCHIVED=0 WHERE Z_PK=?", (pk,))
        elif t == "Fixar":
            st["pinned"] = True
            self._db("UPDATE ZWACHATSESSION SET ZLASTMESSAGEDATE=? WHERE Z_PK=?", (NOW + 86400 * 365 * 7000, pk))
        elif t == "Desafixar":
            st["pinned"] = False
            self._db("UPDATE ZWACHATSESSION SET ZLASTMESSAGEDATE=? WHERE Z_PK=?", (NOW - 60, pk))
        elif t in ("8 horas", "1 semana", "Sempre"):
            st["muted"] = True
            self._db("INSERT INTO ZWACHATPROPERTIES (ZCHATSESSION, ZMUTEDATE) VALUES (?, ?)", (pk, NOW + 8 * 3600))
        elif t == "Reativar som":
            st["muted"] = False
            self._db("UPDATE ZWACHATPROPERTIES SET ZMUTEDATE=NULL WHERE ZCHATSESSION=?", (pk,))
        elif t == "Marcar como não lida":
            st["unread"] = True
            self._db("UPDATE ZWACHATSESSION SET ZUNREADCOUNT=-1 WHERE Z_PK=?", (pk,))
        elif t in ("Ligação de voz", "Ligação de vídeo"):
            self.in_call = self.call_starts
        elif t == "Encerrar ligação":
            if self.hangup_works:
                self.in_call = False
        elif t in self.devices_mic:
            self.mic = t
        elif t in self.devices_out:
            self.out = t

    @staticmethod
    def _fmt(items):
        lines = []
        for it in items:
            name = it[0] if isinstance(it, tuple) else it["title"]
            enabled = True if isinstance(it, tuple) else it["enabled"]
            lines.append(f"{name if name else 'missing value'}\t{'true' if enabled else 'false'}")
        return "\n".join(lines)

    # ---------------------------------------------------------- subprocess

    def _ok(self, cmd, out=""):
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "open":
            q = parse_qs(urlparse(cmd[1]).query)
            phone = q.get("phone", [""])[0]
            self.current_chat = self.chat_by_phone.get(phone)
            return self._ok(cmd)
        assert cmd[0] == "osascript", cmd
        script = cmd[2]
        first = script.splitlines()[0]
        op = first[len("-- wa:"):].split(" ")[0] if first.startswith("-- wa:") else None
        if op is None:
            if "activate" in script:
                op = "activate"
            elif "key code" in script:
                raise AssertionError("unexpected keystroke")
            else:
                op = "front"
        if op in self.stderr_for:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=self.stderr_for[op])
        if op == "activate":
            return self._ok(cmd)
        if op == "front":
            return self._ok(cmd, self.front)
        if op == "list-bar":
            return self._ok(cmd, self._fmt(self.menus()))
        path = [int(x) for x in first.split(" ")[2].split(",")]
        if op == "list-items":
            return self._ok(cmd, self._fmt(self._resolve(path)["sub"]))
        if op == "click":
            if self.front != "WhatsApp":
                return self._ok(cmd, "front:" + self.front)
            self._click(self._resolve(path))
            return self._ok(cmd, "clicked")
        raise AssertionError(f"unknown op {op}")


def make_ui(fake, clock):
    from wa.ui import WhatsAppUI

    return WhatsAppUI(runner=fake, sleep=clock.sleep, clock=clock)


__all__ = ["FakeWhatsApp", "FakeClock", "make_ui", "APPLE"]
