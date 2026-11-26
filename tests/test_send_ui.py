"""The `ui` send method with simulated osascript, `open` and database (nothing real runs)."""

import json
import sqlite3
import subprocess

import pytest

from conftest import APPLE, BRUNO_PHONE
from wa import cli, mcp_server, send
from wa.db import AccessibilityError, AutomationError
from wa.send import send_message

ENV = {"WA_ALLOW_SEND": "1"}


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class FakeUI:
    """Simulates `open`, `osascript` and WhatsApp writing the message to the database."""

    def __init__(self, db_path, clock, fronts=("WhatsApp",), press="pressed", writes=True,
                 chat_pk=2, stderr_on=None, stderr="", text_override=None):
        self.db_path = db_path
        self.clock = clock
        self.fronts = list(fronts)
        self.press = press
        self.writes = writes
        self.chat_pk = chat_pk
        self.stderr_on = stderr_on
        self.stderr = stderr
        self.text_override = text_override
        self.calls = []
        self.pressed = 0
        self.last_url = None

    def _ok(self, cmd, out=""):
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "open":
            self.last_url = cmd[1]
            return self._ok(cmd)
        assert cmd[0] == "osascript", cmd
        script = cmd[2]
        kind = "press" if "key code 36" in script else "front" if "frontmost" in script else "activate"
        if self.stderr_on == kind:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=self.stderr)
        if kind == "activate":
            return self._ok(cmd)
        if kind == "front":
            front = self.fronts.pop(0) if len(self.fronts) > 1 else self.fronts[0]
            return self._ok(cmd, front)
        # press
        if self.press != "pressed":
            return self._ok(cmd, self.press)
        self.pressed += 1
        if self.writes:
            from urllib.parse import parse_qs, urlparse

            text = self.text_override or parse_qs(urlparse(self.last_url).query)["text"][0]
            c = sqlite3.connect(self.db_path)
            c.execute(
                "INSERT INTO ZWAMESSAGE (ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZISFROMME, ZMESSAGETYPE, ZMESSAGESTATUS) "
                "VALUES (?, ?, ?, 1, 0, 8)",
                (self.chat_pk, self.clock() - APPLE + 1, text),
            )
            c.commit()
            c.close()
        return self._ok(cmd, "pressed")


@pytest.fixture
def ui(data_dir):
    clock = FakeClock()

    def make(**kw):
        return FakeUI(data_dir / "ChatStorage.sqlite", clock, **kw)

    return clock, make


def _send(wa, clock, runner, text="Hello, Bruno", to="bruno", **kw):
    return send_message(wa, to, text, confirm=True, env=ENV, runner=runner, sleep=clock.sleep, clock=clock, **kw)


def test_ui_is_default_and_confirms_in_db(wa, ui):
    clock, make = ui
    r = make()
    out = _send(wa, clock, r, text="Hi & see you soon 😀")
    assert out["method"] == "ui"
    assert out["sent"] is True and out["mode"] == "sent" and out["pressed_return"] is True
    conf = out["confirmation"]
    assert conf["status"] == 8 and conf["chat_matched"] is True and conf["chat_jid"].endswith("@lid")
    assert r.calls[0] == ["open", send.draft_url(f"+{BRUNO_PHONE}", "Hi & see you soon 😀")]
    assert r.pressed == 1
    assert send.UI_LOAD_DELAY in clock.sleeps


def test_ui_waits_until_frontmost(wa, ui):
    clock, make = ui
    r = make(fronts=("Terminal", "Terminal", "WhatsApp"))
    out = _send(wa, clock, r)
    assert out["sent"] is True and r.pressed == 1


def test_ui_never_presses_when_not_frontmost(wa, ui):
    clock, make = ui
    r = make(fronts=("Terminal",))
    out = _send(wa, clock, r)
    assert out["sent"] is False and out["reason"] == "whatsapp_not_frontmost"
    assert out["pressed_return"] is False
    assert not any("key code 36" in c[-1] for c in r.calls if c[0] == "osascript")
    assert r.pressed == 0


def test_ui_front_changes_before_press(wa, ui):
    clock, make = ui
    r = make(press="front:Terminal")
    out = _send(wa, clock, r)
    assert out["sent"] is False and out["reason"] == "whatsapp_not_frontmost" and out["pressed_return"] is False


def test_ui_press_script_checks_front_atomically():
    assert "frontmost" in send._PRESS_SCRIPT and "key code 36" in send._PRESS_SCRIPT
    assert send._PRESS_SCRIPT.index('if fp is "WhatsApp"') < send._PRESS_SCRIPT.index("key code 36")


def test_ui_not_confirmed_in_db(wa, ui):
    clock, make = ui
    r = make(writes=False)
    out = _send(wa, clock, r)
    assert out["sent"] is False and out["reason"] == "not_confirmed_in_db" and out["pressed_return"] is True
    assert clock.now >= 1_800_000_000.0 + send.UI_CONFIRM_TIMEOUT


def test_ui_ignores_different_text_and_old_messages(wa, ui, data_dir):
    clock, make = ui
    # Same message, but older than the start: does not count.
    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.execute(
        "INSERT INTO ZWAMESSAGE (ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZISFROMME, ZMESSAGETYPE, ZMESSAGESTATUS) VALUES (2, ?, 'Hello, Bruno', 1, 0, 8)",
        (clock() - APPLE - 3600,),
    )
    c.commit()
    c.close()
    r = make(text_override="outro texto")
    out = _send(wa, clock, r)
    assert out["sent"] is False and out["reason"] == "not_confirmed_in_db"


def test_ui_new_chat_falls_back_to_any_chat(wa, ui):
    clock, make = ui
    r = make(chat_pk=1)  # the message showed up in another chat (unknown JID)
    out = _send(wa, clock, r)
    assert out["sent"] is True and out["confirmation"]["chat_matched"] is False


@pytest.mark.parametrize(
    "stage,stderr",
    [
        ("press", "execution error: System Events got an error: osascript is not allowed to send keystrokes. (1002)"),
        ("press", "System Events got an error: osascript is not allowed assistive access. (-25211)"),
        ("front", "execution error: System Events got an error: (-1719)"),
    ],
)
def test_ui_accessibility_error(wa, ui, stage, stderr):
    clock, make = ui
    r = make(stderr_on=stage, stderr=stderr)
    with pytest.raises(AccessibilityError) as exc:
        _send(wa, clock, r)
    assert "Accessibility" in exc.value.message
    assert exc.value.to_dict()["error"] == "accessibility_required"
    assert r.pressed == 0


def test_ui_automation_error(wa, ui):
    clock, make = ui
    r = make(stderr_on="activate", stderr="Not authorized to send Apple events to WhatsApp. (-1743)")
    with pytest.raises(AutomationError):
        _send(wa, clock, r)


def test_ui_requires_both_locks(wa, ui):
    clock, make = ui
    r = make()
    out = send_message(wa, "bruno", "hi", env=ENV, runner=r, sleep=clock.sleep, clock=clock)
    assert out["sent"] is False and r.calls == []  # no confirm: draft
    with pytest.raises(send.SendRefused):
        send_message(wa, "bruno", "hi", confirm=True, env={}, runner=r, sleep=clock.sleep, clock=clock)
    assert r.calls == []


def test_cli_send_ui_default(data_dir, capsys, monkeypatch, ui):
    clock, make = ui
    r = make()
    real = send.send_message

    def patched(*a, **kw):
        kw.update(runner=r, sleep=clock.sleep, clock=clock, env=ENV)
        return real(*a, **kw)

    monkeypatch.setattr(cli, "send_message", patched)
    rc = cli.run(["send", "--to", "bruno", "--text", "hi cli", "--confirm"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["sent"] is True and out["method"] == "ui"

    r2 = make(fronts=("Terminal",))

    def patched2(*a, **kw):
        kw.update(runner=r2, sleep=clock.sleep, clock=clock, env=ENV)
        return real(*a, **kw)

    monkeypatch.setattr(cli, "send_message", patched2)
    rc = cli.run(["send", "--to", "bruno", "--text", "hi cli 2", "--confirm"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["sent"] is False


def test_mcp_send_method_validation(data_dir):
    assert mcp_server.wa_send_message("bruno", "hi", method="draft")["error"] == "invalid_argument"
    assert mcp_server.wa_send_message("bruno", "hi", confirm=True)["error"] == "send_refused"
