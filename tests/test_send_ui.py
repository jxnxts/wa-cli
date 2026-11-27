"""The `ui` send method with simulated osascript, `open` and database (nothing real runs)."""

import json
import sqlite3
import subprocess

import pytest

from conftest import APPLE, BRUNO_PHONE, GROUP_JID
from fake_whatsapp import IOREG_LOCKED, IOREG_UNLOCKED, ioreg_result
from wa import cli, mcp_server, send
from wa.db import AccessibilityError, AutomationError, ScreenLockedError, SendFailed
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
                 chat_pk=2, stderr_on=None, stderr="", text_override=None, locked=False,
                 clipboard="previous content", timeout_on=None):
        self.db_path = db_path
        self.clock = clock
        self.fronts = list(fronts)
        self.press = press
        self.writes = writes
        self.chat_pk = chat_pk
        self.stderr_on = stderr_on
        self.stderr = stderr
        self.text_override = text_override
        self.locked = locked
        self.clipboard = clipboard
        self.clipboard_history = []
        self.timeout_on = timeout_on  # step whose osascript "hangs" (TimeoutExpired)
        self.pasted = None
        self.calls = []
        self.pressed = 0
        self.last_url = None

    def _ok(self, cmd, out=""):
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "ioreg":
            return ioreg_result(cmd, self.locked)
        if cmd[0] == "pbpaste":  # like the real one: no trailing newline
            return subprocess.CompletedProcess(cmd, 0, stdout=self.clipboard, stderr="")
        if cmd[0] == "pbcopy":
            self.clipboard = kw["input"]
            self.clipboard_history.append(kw["input"])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "open":
            self.last_url = cmd[1]
            return self._ok(cmd)
        assert cmd[0] == "osascript", cmd
        script = cmd[2]
        kind = (
            "press" if "key code 36" in script
            else "paste" if 'keystroke "v"' in script
            else "front" if "frontmost" in script
            else "activate"
        )
        if self.timeout_on == kind:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if kind == "paste":
            if self.press != "pressed":
                return self._ok(cmd, self.press)
            self.pasted = self.clipboard
            return self._ok(cmd, "pasted")
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

            if self.last_url.startswith("whatsapp://openchat/"):
                text = self.text_override or self.pasted
            else:
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
    assert r.calls[0] == send.IOREG_CMD  # locked screen? (before any System Events)
    assert r.calls[1] == ["open", send.draft_url(f"+{BRUNO_PHONE}", "Hi & see you soon 😀")]
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


# ------------------------------------------------------------ groups


def test_ui_group_pastes_presses_and_confirms_by_group_jid(wa, ui, monkeypatch):
    clock, make = ui
    r = make(chat_pk=3)  # the Família group chat
    monkeypatch.setattr(wa, "jids_for_phone", lambda *_: pytest.fail("a group has no phone number"))
    out = _send(wa, clock, r, to=GROUP_JID, text="Good morning, family 😀")
    assert out["sent"] is True and out["mode"] == "sent" and out["pressed_return"] is True
    assert out["to"]["kind"] == "group" and out["to"]["phone"] is None
    assert out["confirmation"]["chat_jid"] == GROUP_JID and out["confirmation"]["chat_matched"] is True
    assert "warning" not in out
    assert ["open", f"whatsapp://openchat/{GROUP_JID}"] in r.calls
    assert r.pasted == "Good morning, family 😀" and r.pressed == 1
    # Clipboard: the message text during the send, then the previous content.
    assert r.clipboard_history == ["Good morning, family 😀", "previous content"]
    assert r.clipboard == "previous content" and out["clipboard_restored"] is True
    # Order: paste before Return.
    scripts = [c[2] for c in r.calls if c[0] == "osascript"]
    paste_i = next(i for i, sc in enumerate(scripts) if 'keystroke "v"' in sc)
    press_i = next(i for i, sc in enumerate(scripts) if "key code 36" in sc)
    assert paste_i < press_i


def test_ui_group_paste_checks_front_atomically():
    s = send._PASTE_SCRIPT
    assert s.index('if fp is "WhatsApp"') < s.index('keystroke "v"')


def test_ui_group_not_frontmost_pastes_nothing(wa, ui):
    clock, make = ui
    r = make(chat_pk=3, fronts=("Terminal",))
    out = _send(wa, clock, r, to=GROUP_JID)
    assert out["sent"] is False and out["reason"] == "whatsapp_not_frontmost"
    assert r.pasted is None and r.pressed == 0
    assert r.clipboard == "previous content"  # restored even without a send


def test_ui_group_message_in_other_chat_warns(wa, ui):
    clock, make = ui
    r = make(chat_pk=1)  # showed up in Ana's chat, not in the group
    out = _send(wa, clock, r, to=GROUP_JID)
    assert out["sent"] is True and out["confirmation"]["chat_matched"] is False
    assert GROUP_JID in out["warning"]


def test_routing_desktop_sends_to_group(wa, ui):
    from wa import routing

    clock, make = ui
    r = make(chat_pk=3)
    out = routing.send(wa, GROUP_JID, "Hello, group", confirm=True, via="desktop", env=ENV,
                       sleep=clock.sleep, clock=clock, runner=r)
    assert out["via"] == "desktop" and out["sent"] is True and out["to"]["kind"] == "group"


# ------------------------------------------------------------ locked screen / timeouts


def test_parse_screen_locked():
    assert send.parse_screen_locked(IOREG_LOCKED) is True
    assert send.parse_screen_locked(IOREG_UNLOCKED) is False
    # The console session key alone (without IOConsoleLocked) also counts.
    only_session = '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes,"CGSSessionScreenIsLocked"=Yes})'
    assert send.parse_screen_locked(only_session) is True
    # Another session (fast user switching) being locked does not count.
    other = '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=No,"CGSSessionScreenIsLocked"=Yes},{"kCGSSessionOnConsoleKey"=Yes})'
    assert send.parse_screen_locked(other) is False


def test_screen_locked_unknown_when_ioreg_fails():
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    def hanging(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    assert send.screen_locked(failing) is None
    assert send.screen_locked(hanging) is None
    send.ensure_screen_unlocked(failing)  # unknown: does not block


@pytest.mark.parametrize("to", ["bruno", GROUP_JID])
def test_ui_screen_locked_fails_fast(wa, ui, to):
    clock, make = ui
    r = make(chat_pk=3, locked=True)
    with pytest.raises(ScreenLockedError) as exc:
        _send(wa, clock, r, to=to)
    assert exc.value.to_dict()["error"] == "screen_locked"
    assert "screen is locked" in exc.value.message and "Nothing was sent" in exc.value.message
    assert r.calls == [send.IOREG_CMD]  # no open, no osascript, no clipboard
    assert clock.sleeps == []


def test_cli_send_screen_locked(data_dir, capsys, monkeypatch, ui):
    clock, make = ui
    r = make(locked=True)
    real = send.send_message

    def patched(*a, **kw):
        kw.update(runner=r, sleep=clock.sleep, clock=clock, env=ENV)
        return real(*a, **kw)

    monkeypatch.setattr(cli, "send_message", patched)
    rc = cli.run(["send", "--to", "bruno", "--text", "hi", "--confirm", "--via", "desktop"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "screen_locked"


def test_ui_system_events_timeout_message(wa, ui):
    clock, make = ui
    r = make(timeout_on="front")
    with pytest.raises(SendFailed) as exc:
        _send(wa, clock, r)
    msg = exc.value.message
    assert "timeout" in msg and "locked" in msg and "Accessibility/Automation" in msg
    assert msg.endswith("Nothing was sent.")
    assert exc.value.details["reason"] == "osascript_timeout"
    assert r.pressed == 0


def test_ui_press_timeout_checks_db_before_claiming_nothing_sent(wa, ui):
    clock, make = ui
    r = make(timeout_on="press")
    with pytest.raises(SendFailed) as exc:
        _send(wa, clock, r)
    assert "Nothing was sent" in exc.value.message


def test_ui_press_timeout_but_message_in_db_is_sent(wa, ui, data_dir):
    clock, make = ui
    r = make(timeout_on="press")
    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.execute(
        "INSERT INTO ZWAMESSAGE (ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZISFROMME, ZMESSAGETYPE, ZMESSAGESTATUS) "
        "VALUES (2, ?, 'Hello, Bruno', 1, 0, 8)",
        (clock() - APPLE + 1,),
    )
    c.commit()
    c.close()
    out = _send(wa, clock, r)
    assert out["sent"] is True and out["confirmation"]["chat_matched"] is True


def test_osascript_app_timeout_message():
    def hanging(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    with pytest.raises(SendFailed) as exc:
        send.osascript(hanging, 'tell application "WhatsApp" to activate')
    assert "WhatsApp did not answer" in exc.value.message and exc.value.message.endswith("Nothing was sent.")


def test_ui_group_copies_the_text_only_with_whatsapp_in_front(wa, ui):
    """Copying before the open and the waits left seconds in which whatever the user copied
    would be pasted and sent to the group."""
    clock, make = ui
    r = make(chat_pk=3)
    out = _send(wa, clock, r, to=GROUP_JID, text="hi group")
    assert out["sent"] is True
    open_i = next(i for i, c in enumerate(r.calls) if c[0] == "open")
    copy_i = next(i for i, c in enumerate(r.calls) if c[0] == "pbcopy")
    paste_i = next(i for i, c in enumerate(r.calls) if c[0] == "osascript" and 'keystroke "v"' in c[2])
    last_front_i = max(i for i, c in enumerate(r.calls[:paste_i]) if c[0] == "osascript" and c[2] == send._FRONT_SCRIPT)
    assert open_i < last_front_i < copy_i < paste_i


def test_ui_group_never_touches_the_clipboard_when_whatsapp_stays_behind(wa, ui):
    clock, make = ui
    r = make(chat_pk=3, fronts=("Terminal",))
    out = _send(wa, clock, r, to=GROUP_JID)
    assert out["sent"] is False and r.clipboard_history == [] and "clipboard_restored" not in out
