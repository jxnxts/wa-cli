"""Chat management actions, calls and the related reads (simulated WhatsApp, pt-BR and English UI)."""

import json

import pytest

from conftest import ANA_JID, BRUNO_LID, BRUNO_PHONE, GROUP_JID
from fake_whatsapp import FakeClock, FakeWhatsApp, make_ui
from wa import actions, cli, mcp_server
from wa.actions import chat_action, hangup, start_call
from wa.core import WhatsApp
from wa.db import AccessibilityError, ActionFailed, ActionRefused, InvalidArgument
from wa.ui import norm_title, script_click

ACT = {"WA_ALLOW_ACTIONS": "1"}
CALLS = {"WA_ALLOW_CALLS": "1"}
PHONES = {"5561911112222": 1, BRUNO_PHONE: 2}


@pytest.fixture
def env(data_dir):
    clock = FakeClock()

    def make(**kw):
        fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES, **kw)
        return fake, make_ui(fake, clock)

    return make


# ------------------------------------------------------------ helpers


def test_norm_title_strips_lrm_and_ellipsis():
    assert norm_title("‎Bloquear…") == "bloquear"
    assert norm_title("‎Arquivar") == "arquivar"


def test_click_script_checks_front_before_click():
    s = script_click([4, 5, 2])
    assert s.index('if fp is not "WhatsApp"') < s.index("click menu item 2 of menu 1 of menu item 5 of menu 1 of menu bar item 4")


# ------------------------------------------------------------ plan / guards


def test_plan_without_confirm_touches_nothing(wa, env):
    fake, ui = env()
    out = chat_action(wa, "bruno", "archive", env=ACT, ui=ui)
    assert out["mode"] == "plan" and out["done"] is False
    assert out["state_before"]["archived"] is False and out["state_before"]["chat_jid"] == BRUNO_LID
    assert any("Archive" in step for step in out["plan"])
    assert fake.calls == []


def test_actions_need_env(wa, env):
    fake, ui = env()
    with pytest.raises(ActionRefused):
        chat_action(wa, "bruno", "archive", confirm=True, env={}, ui=ui)
    with pytest.raises(ActionRefused):
        chat_action(wa, "bruno", "archive", confirm=True, env=CALLS, ui=ui)
    assert fake.calls == []


def test_invalid_action_and_groups(wa, env):
    fake, ui = env()
    with pytest.raises(InvalidArgument):
        chat_action(wa, "bruno", "block", confirm=True, env=ACT, ui=ui)
    with pytest.raises(InvalidArgument) as exc:
        chat_action(wa, GROUP_JID, "archive", confirm=True, env=ACT, ui=ui)
    assert "Groups" in exc.value.message
    assert fake.calls == []


# ------------------------------------------------------------ execution


@pytest.mark.parametrize(
    "action,clicked,key,expected",
    [
        ("archive", "Arquivar", "archived", True),
        ("pin", "Fixar", "pinned", True),
        ("mute-8h", "8 horas", "muted", True),
        ("mute-always", "Sempre", "muted", True),
        ("mark-unread", "Marcar como não lida", "unread", -1),
    ],
)
def test_action_clicks_and_verifies(wa, env, action, clicked, key, expected):
    fake, ui = env()
    out = chat_action(wa, "bruno", action, confirm=True, env=ACT, ui=ui)
    assert out["done"] is True and out["clicked"] == clicked and out["verified"] is True
    assert out["state_after"][key] == expected
    assert fake.clicked == [clicked]
    assert fake.calls[0] == ["open", f"whatsapp://send?phone={BRUNO_PHONE}"]


@pytest.mark.parametrize(
    "action,clicked,key,expected",
    [
        ("archive", "Archive", "archived", True),
        ("pin", "Pin", "pinned", True),
        ("mute-1w", "1 week", "muted", True),
        ("mark-unread", "Mark as unread", "unread", -1),
    ],
)
def test_action_with_english_ui(wa, env, action, clicked, key, expected):
    fake, ui = env(lang="en")
    out = chat_action(wa, "bruno", action, confirm=True, env=ACT, ui=ui)
    assert out["done"] is True and out["clicked"] == clicked and out["verified"] is True
    assert out["state_after"][key] == expected
    assert fake.clicked == [clicked]


def test_english_ui_toggle_and_unmute(wa, env):
    fake, ui = env(lang="en")
    chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    out = chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert out["already"] is True and "Unarchive" in out["note"]
    out = chat_action(wa, "bruno", "unarchive", confirm=True, env=ACT, ui=ui)
    assert out["clicked"] == "Unarchive" and out["verified"] is True
    chat_action(wa, "bruno", "mute-8h", confirm=True, env=ACT, ui=ui)
    out = chat_action(wa, "bruno", "unmute", confirm=True, env=ACT, ui=ui)
    assert out["clicked"] == "Unmute sound" and out["verified"] is True


def test_toggle_already_in_state(wa, env, data_dir):
    import sqlite3

    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.execute("UPDATE ZWACHATSESSION SET ZARCHIVED=1 WHERE Z_PK=2")
    c.commit()
    c.close()
    fake, ui = env()
    out = chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert out["already"] is True and out["clicked"] is None and fake.clicked == []
    out = chat_action(wa, "bruno", "unarchive", confirm=True, env=ACT, ui=ui)
    assert out["clicked"] == "Desarquivar" and out["verified"] is True


def test_unmute_after_mute(wa, env):
    fake, ui = env()
    chat_action(wa, "bruno", "mute-1w", confirm=True, env=ACT, ui=ui)
    out = chat_action(wa, "bruno", "unmute", confirm=True, env=ACT, ui=ui)
    assert out["clicked"] == "Reativar som" and out["verified"] is True


def test_not_verified_when_db_does_not_change(wa, env):
    fake, ui = env(write_db=False)
    out = chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert out["done"] is True and out["verified"] is False and out["reason"] == "not_seen_in_db"


def test_first_click_does_not_take_retries_once(wa, data_dir):
    import sqlite3

    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.execute("UPDATE ZWACHATSESSION SET ZARCHIVED=1 WHERE Z_PK=2")
    c.commit()
    c.close()
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES, ignore_clicks=1)
    out = chat_action(wa, "bruno", "unarchive", confirm=True, env=ACT, ui=make_ui(fake, clock))
    assert out["verified"] is True and out["attempts"] == 2
    assert fake.clicked == ["Desarquivar", "Desarquivar"]
    assert out["state_after"]["archived"] is False
    assert actions.SETTLE_DELAY in clock.sleeps and actions.RETRY_DELAY in clock.sleeps


def test_retry_is_the_last_attempt(wa, data_dir):
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES, ignore_clicks=5)
    out = chat_action(wa, "bruno", "mute-8h", confirm=True, env=ACT, ui=make_ui(fake, clock))
    assert out["verified"] is False and out["attempts"] == 2 and out["reason"] == "not_seen_in_db"
    assert fake.clicked == ["8 horas", "8 horas"]


def test_no_retry_when_menu_already_changed(wa, env):
    fake, ui = env(write_db=False)
    out = chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert out["verified"] is False and out["attempts"] == 1 and "retry_skipped" in out
    assert fake.clicked == ["Arquivar"]


def test_settle_delay_before_first_click(wa, data_dir):
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    out = chat_action(wa, "bruno", "pin", confirm=True, env=ACT, ui=make_ui(fake, clock))
    assert out["attempts"] == 1 and out["verified"] is True
    assert clock.sleeps[:2] == [actions.UI_LOAD_DELAY, actions.SETTLE_DELAY]


def test_chat_not_in_db(wa, data_dir):
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", {"5521900001111": 99})
    out = chat_action(wa, "+55 21 90000-1111", "archive", confirm=True, env=ACT, ui=make_ui(fake, clock))
    assert out["done"] is True and out["verified"] is False and out["reason"] == "chat_not_in_db"


def test_missing_chat_menu(wa, data_dir):
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", {})
    with pytest.raises(ActionFailed):
        chat_action(wa, "+55 21 90000-1111", "archive", confirm=True, env=ACT, ui=make_ui(fake, clock))
    assert fake.clicked == []


def test_unknown_ui_language_lists_menu_titles(wa, env, monkeypatch):
    fake, ui = env()
    monkeypatch.setattr(actions, "MENU_CHAT", ("Unterhaltung",))
    with pytest.raises(ActionFailed) as exc:
        chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert "Conversa" in exc.value.details["menu_items"]
    assert fake.clicked == []


def test_never_clicks_when_not_frontmost(wa, env):
    fake, ui = env(front="Terminal")
    out = chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert out["done"] is False and out["reason"] == "whatsapp_not_frontmost"
    assert fake.clicked == []
    assert not any("wa:click" in c[-1] for c in fake.calls if c[0] == "osascript")


def test_accessibility_error(wa, env):
    fake, ui = env(stderr_for={"list-bar": "System Events got an error: osascript is not allowed assistive access. (-25211)"})
    with pytest.raises(AccessibilityError):
        chat_action(wa, "bruno", "archive", confirm=True, env=ACT, ui=ui)
    assert fake.clicked == []


# ------------------------------------------------------------ calls


def test_call_plan_and_locks(wa, env):
    fake, ui = env()
    out = start_call(wa, "bruno", "video", env=CALLS, ui=ui)
    assert out["mode"] == "plan" and fake.calls == []
    with pytest.raises(ActionRefused):
        start_call(wa, "bruno", confirm=True, env=ACT, ui=ui)
    with pytest.raises(ActionRefused):
        hangup(confirm=True, env=ACT, ui=ui)
    with pytest.raises(InvalidArgument):
        start_call(wa, "bruno", "fax", env=CALLS, ui=ui)


def test_call_and_hangup(wa, env):
    fake, ui = env()
    out = start_call(wa, "bruno", "voice", confirm=True, env=CALLS, ui=ui)
    assert out["started"] is True and out["verified"] is True and fake.in_call
    assert fake.clicked == ["Ligação de voz"]
    with pytest.raises(ActionFailed):
        start_call(wa, "bruno", "voice", confirm=True, env=CALLS, ui=ui)  # already in a call
    out = hangup(confirm=True, env=CALLS, ui=ui)
    assert out["done"] is True and out["verified"] is True and not fake.in_call


def test_call_not_verified(wa, env):
    fake, ui = env(call_starts=False)
    out = start_call(wa, "bruno", "voice", confirm=True, env=CALLS, ui=ui)
    assert out["started"] is True and out["verified"] is False and out["reason"] == "hangup_item_not_enabled"


def test_hangup_without_call(wa, env):
    fake, ui = env()
    out = hangup(confirm=True, env=CALLS, ui=ui)
    assert out["done"] is False and out["reason"] == "no_active_call"


def test_select_call_device(wa, env):
    fake, ui = env()
    start_call(wa, "bruno", "voice", confirm=True, env=CALLS, ui=ui)
    assert actions.select_call_device(ui, actions.SUBMENU_MICROPHONE, "BlackHole 16ch") == "BlackHole 16ch"
    assert actions.select_call_device(ui, actions.SUBMENU_SPEAKER, "blackhole 2ch") == "BlackHole 2ch"
    assert fake.mic == "BlackHole 16ch" and fake.out == "BlackHole 2ch"
    with pytest.raises(ActionFailed) as exc:
        actions.select_call_device(ui, actions.SUBMENU_MICROPHONE, "Loopback")
    assert "BlackHole 16ch" in exc.value.details["devices"]


def test_call_devices_and_hangup_with_english_ui(wa, env):
    fake, ui = env(lang="en")
    out = start_call(wa, "bruno", "video", confirm=True, env=CALLS, ui=ui)
    assert out["started"] is True and out["verified"] is True and fake.clicked == ["Video call"]
    assert actions.select_call_device(ui, actions.SUBMENU_MICROPHONE, "BlackHole 16ch") == "BlackHole 16ch"
    assert actions.select_call_device(ui, actions.SUBMENU_SPEAKER, "BlackHole 2ch") == "BlackHole 2ch"
    out = hangup(confirm=True, env=CALLS, ui=ui)
    assert out["done"] is True and out["verified"] is True and fake.clicked[-1] == "End Call"


# ------------------------------------------------------------ reads


def test_chats_archived_filter(data_dir):
    import sqlite3

    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.execute("UPDATE ZWACHATSESSION SET ZARCHIVED=1 WHERE Z_PK=2")
    c.commit()
    c.close()
    with WhatsApp() as w:
        assert [x["jid"] for x in w.list_chats(archived=True)["chats"]] == [BRUNO_LID]
        assert BRUNO_LID not in [x["jid"] for x in w.list_chats(archived=False)["chats"]]


def test_list_calls(wa):
    out = wa.list_calls()
    assert out["count"] == 2
    first = out["calls"][0]
    assert first["missed"] is True and first["video"] is True and first["direction"] == "incoming"
    assert first["participants"][0]["phone"] == f"+{BRUNO_PHONE}"
    assert out["calls"][1]["duration_s"] == 62.5
    assert out["calls"][1]["participants"][0]["jid"] == ANA_JID
    assert [c["id"] for c in wa.list_calls(missed=True)["calls"]] == [2]


def test_list_lists_empty_and_filled(wa, data_dir):
    assert wa.list_lists() == {"count": 0, "lists": []}
    import sqlite3

    c = sqlite3.connect(data_dir / "Labels.sqlite")
    c.execute("INSERT INTO ZWASMBLABEL (Z_PK, ZLABELID, ZNAME, ZTYPE, ZISACTIVE) VALUES (1, 10, 'Work', 0, 1)")
    c.execute("INSERT INTO ZWASMBLABELEDENTITYPROXY (Z_PK, ZCONTACTJID) VALUES (1, ?)", (ANA_JID,))
    c.execute("INSERT INTO Z_1PROXIES VALUES (1, 1)")
    c.commit()
    c.close()
    c = sqlite3.connect(data_dir / "BackedUpStorageDB" / "BackedUpStorageDatabase.sqlite")
    c.execute("INSERT INTO list_member VALUES (10, ?)", (BRUNO_LID,))
    c.commit()
    c.close()
    with WhatsApp() as w:
        out = w.list_lists()
    assert out["count"] == 1
    lst = out["lists"][0]
    assert lst["name"] == "Work" and {m["jid"] for m in lst["members"]} == {ANA_JID, BRUNO_LID}


def test_lists_missing_dbs(tmp_path, monkeypatch):
    from conftest import build_dbs

    base = build_dbs(tmp_path / "w")
    (base / "Labels.sqlite").unlink()
    (base / "CallHistory.sqlite").unlink()
    monkeypatch.setenv("WA_DATA_DIR", str(base))
    with WhatsApp() as w:
        assert w.list_lists()["count"] == 0
        assert w.list_calls()["count"] == 0


# ------------------------------------------------------------ CLI and MCP


def _cli(capsys, *argv):
    rc = cli.run(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def test_cli_actions_and_calls(data_dir, capsys, monkeypatch):
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    monkeypatch.setattr(cli, "_ui_factory", lambda: make_ui(fake, clock))
    rc, out = _cli(capsys, "chat-action", "--to", "bruno", "--action", "pin")
    assert rc == 0 and out["mode"] == "plan" and fake.calls == []
    rc, out = _cli(capsys, "chat-action", "--to", "bruno", "--action", "pin", "--confirm")
    assert rc == 1 and out["error"] == "action_refused"
    monkeypatch.setenv("WA_ALLOW_ACTIONS", "1")
    rc, out = _cli(capsys, "chat-action", "--to", "bruno", "--action", "pin", "--confirm")
    assert rc == 0 and out["verified"] is True
    rc, out = _cli(capsys, "call", "--to", "bruno", "--confirm")
    assert rc == 1 and out["error"] == "action_refused"
    monkeypatch.setenv("WA_ALLOW_CALLS", "1")
    rc, out = _cli(capsys, "call", "--to", "bruno", "--kind", "video", "--confirm")
    assert rc == 0 and out["started"] is True
    rc, out = _cli(capsys, "hangup", "--confirm")
    assert rc == 0 and out["done"] is True
    rc, out = _cli(capsys, "calls", "--missed")
    assert out["count"] == 1
    rc, out = _cli(capsys, "lists")
    assert out == {"count": 0, "lists": []}
    rc, out = _cli(capsys, "chats", "--archived")
    assert rc == 0 and out["count"] == 0


def test_mcp_actions_and_calls(data_dir, monkeypatch):
    clock = FakeClock()
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    monkeypatch.setattr(mcp_server, "_ui", lambda: make_ui(fake, clock))
    assert mcp_server.wa_chat_action("bruno", "archive")["mode"] == "plan"
    assert mcp_server.wa_chat_action("bruno", "archive", confirm=True)["error"] == "action_refused"
    assert mcp_server.wa_call("bruno", confirm=True)["error"] == "action_refused"
    assert mcp_server.wa_hangup(confirm=True)["error"] == "action_refused"
    monkeypatch.setenv("WA_ALLOW_ACTIONS", "1")
    assert mcp_server.wa_chat_action("bruno", "archive", confirm=True)["verified"] is True
    assert mcp_server.wa_list_chats(archived=True)["chats"][0]["jid"] == BRUNO_LID
    assert mcp_server.wa_list_calls(missed_only=True)["count"] == 1
    assert mcp_server.wa_list_lists()["count"] == 0
    assert fake.clicked == ["Arquivar"]
