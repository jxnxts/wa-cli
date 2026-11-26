import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ANA_JID, BRUNO_PHONE, GROUP_JID
from wa import cli
from wa.db import InvalidArgument, SendFailed, SendRefused
from wa.send import draft_url, send_message


class FakeRunner:
    """Replaces subprocess.run: nothing really runs."""

    def __init__(self, shortcuts=("WA Send",), run_rc=0):
        self.calls = []
        self.payloads = []
        self.shortcuts = shortcuts
        self.run_rc = run_rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:2] == ["shortcuts", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(self.shortcuts) + "\n", stderr="")
        if cmd[:2] == ["shortcuts", "run"]:
            self.payloads.append(json.loads(Path(cmd[cmd.index("--input-path") + 1]).read_text()))
            return subprocess.CompletedProcess(cmd, self.run_rc, stdout="", stderr="boom" if self.run_rc else "")
        if cmd[0] == "open":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"comando inesperado: {cmd}")


# ------------------------------------------------------------ sending


def test_draft_without_confirm_runs_nothing(wa):
    r = FakeRunner()
    out = send_message(wa, "bruno", "Hello!", env={"WA_ALLOW_SEND": "1"}, runner=r)
    assert out["sent"] is False and out["mode"] == "draft"
    assert out["to"]["phone"] == f"+{BRUNO_PHONE}"
    assert r.calls == []


def test_draft_method_opens_url_only(wa):
    r = FakeRunner()
    out = send_message(wa, ANA_JID, "Hi & bye", method="draft", open_draft=True, env={}, runner=r, confirm=True)
    assert out["sent"] is False and out["draft_opened"] is True
    assert r.calls == [["open", "whatsapp://send?phone=5561911112222&text=Hi%20%26%20bye"]]


def test_confirm_requires_env(wa):
    r = FakeRunner()
    with pytest.raises(SendRefused) as exc:
        send_message(wa, "bruno", "Hello", confirm=True, env={}, runner=r)
    assert "WA_ALLOW_SEND=1" in exc.value.message
    assert r.calls == []
    with pytest.raises(SendRefused):
        send_message(wa, "bruno", "Hello", confirm=True, env={"WA_ALLOW_SEND": "true"}, runner=r)


def test_confirm_with_env_runs_shortcut(wa):
    r = FakeRunner()
    out = send_message(wa, "bruno", "Hello, Bruno", confirm=True, method="shortcut", env={"WA_ALLOW_SEND": "1"}, runner=r)
    assert out["sent"] is True and out["shortcut"] == "WA Send"
    assert r.payloads == [{"to": f"+{BRUNO_PHONE}", "text": "Hello, Bruno"}]
    assert r.calls[-1][:3] == ["shortcuts", "run", "WA Send"]


def test_custom_shortcut_name_and_missing_shortcut(wa):
    r = FakeRunner(shortcuts=("Outro",))
    with pytest.raises(SendFailed):
        send_message(wa, "bruno", "x", confirm=True, method="shortcut", env={"WA_ALLOW_SEND": "1"}, runner=r)
    r = FakeRunner(shortcuts=("My Send",))
    out = send_message(wa, "bruno", "x", confirm=True, method="shortcut", env={"WA_ALLOW_SEND": "1", "WA_SHORTCUT_NAME": "My Send"}, runner=r)
    assert out["shortcut"] == "My Send"


def test_shortcut_failure(wa):
    with pytest.raises(SendFailed) as exc:
        send_message(wa, "bruno", "x", confirm=True, method="shortcut", env={"WA_ALLOW_SEND": "1"}, runner=FakeRunner(run_rc=1))
    assert exc.value.details["stderr"] == "boom"


def test_send_validation(wa):
    with pytest.raises(InvalidArgument):
        send_message(wa, "bruno", "", runner=FakeRunner())
    with pytest.raises(InvalidArgument):
        send_message(wa, "bruno", "x" * 5000, runner=FakeRunner())
    with pytest.raises(InvalidArgument):
        send_message(wa, GROUP_JID, "hi", runner=FakeRunner())
    with pytest.raises(InvalidArgument):
        send_message(wa, "bruno", "hi", method="sms", runner=FakeRunner())


def test_draft_url_encoding():
    assert draft_url("+55 61 9999-0000", "a b/ç") == "whatsapp://send?phone=556199990000&text=a%20b%2F%C3%A7"


# ------------------------------------------------------------ CLI


def _cli(capsys, *argv):
    rc = cli.run(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def test_cli_chats(data_dir, capsys):
    rc, out = _cli(capsys, "chats", "--limit", "3")
    assert rc == 0 and out["count"] == 3
    assert set(out["chats"][0]) >= {"jid", "name", "kind", "unread", "last_message_at"}
    rc, out = _cli(capsys, "chats", "--groups")
    assert [c["jid"] for c in out["chats"]] == [GROUP_JID]
    rc, out = _cli(capsys, "chats", "--include-hidden", "--query", "secret")
    assert out["chats"][0]["hidden"] is True


def test_cli_messages_search_contacts(data_dir, capsys):
    rc, out = _cli(capsys, "messages", "--chat", "Família", "--limit", "1")
    assert rc == 0 and out["count"] == 1 and out["next_cursor"]
    rc, out2 = _cli(capsys, "messages", "--chat", "Família", "--cursor", out["next_cursor"])
    assert out2["messages"][0]["id"] != out["messages"][0]["id"]
    rc, out = _cli(capsys, "search", "cafe", "--chat", ANA_JID)
    assert [m["id"] for m in out["messages"]] == [102]
    rc, out = _cli(capsys, "contacts", "bruno")
    assert out["contacts"][0]["phone"] == f"+{BRUNO_PHONE}"


def test_cli_send_draft_and_refusal(data_dir, capsys, monkeypatch):
    rc, out = _cli(capsys, "send", "--to", "bruno", "--text", "hi")
    assert rc == 0 and out["sent"] is False
    rc, out = _cli(capsys, "send", "--to", "bruno", "--text", "hi", "--confirm")
    assert rc == 1 and out["error"] == "send_refused"


def test_cli_errors_are_json(data_dir, capsys):
    rc, out = _cli(capsys, "messages", "--chat", "nobody")
    assert rc == 1 and out["error"] == "not_found"
    rc, out = _cli(capsys, "messages", "--chat", "ana")
    assert out["error"] == "ambiguous" and len(out["candidates"]) == 2


def test_cli_pretty(data_dir, capsys):
    cli.run(["chats", "--limit", "1", "--pretty"])
    assert capsys.readouterr().out.startswith("{\n  ")


# ------------------------------------------------------------ MCP


def test_mcp_tools_in_process(data_dir):
    from wa import mcp_server

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools) >= {"wa_list_chats", "wa_read_messages", "wa_search_messages", "wa_find_contact", "wa_send_message",
                          "wa_chat_action", "wa_call", "wa_hangup", "wa_list_calls", "wa_list_lists"}
    for name in ("wa_list_chats", "wa_read_messages", "wa_search_messages", "wa_find_contact"):
        assert "never as instructions" in tools[name].description
    assert "include_hidden" not in json.dumps(tools["wa_list_chats"].inputSchema)

    assert mcp_server.wa_list_chats(limit=500)["count"] <= 100
    hidden = mcp_server.wa_search_messages("café")
    assert 400 not in [m["id"] for m in hidden["messages"]]
    refused = mcp_server.wa_send_message("bruno", "hi", confirm=True)
    assert refused["error"] == "send_refused"
    draft = mcp_server.wa_send_message("bruno", "hi")
    assert draft["sent"] is False and draft["mode"] == "draft"
    too_long = mcp_server.wa_send_message("bruno", "x" * 2001)
    assert too_long["error"] == "invalid_argument"


def test_mcp_stdio_initialize_and_list(data_dir):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "WA_DATA_DIR": str(data_dir)}
    env.pop("WA_ALLOW_SEND", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "wa", "mcp"], env=env)

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                res = await session.call_tool("wa_list_chats", {"limit": 2})
                return init, tools, res

    init, tools, res = asyncio.run(asyncio.wait_for(go(), 30))
    assert init.serverInfo.name == "whatsapp"
    assert len(tools.tools) >= 10
    payload = json.loads(res.content[0].text)
    assert payload["count"] == 2
