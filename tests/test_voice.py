"""Voice agent. Everything is simulated: WhatsApp, CoreAudio, WebSocket and OpenAI."""

import asyncio
import base64
import json
import subprocess

import pytest

from conftest import BRUNO_PHONE
from fake_whatsapp import FakeClock, FakeWhatsApp, make_ui
from wa import cli, mcp_server, voice
from wa.db import ActionRefused, InvalidArgument, MissingDependency
from wa.voice import VoiceDeps, VoiceSession

SECRET = "sk-test-NOT-A-REAL-KEY"
CALLS = {"WA_ALLOW_CALLS": "1"}
PHONES = {BRUNO_PHONE: 2}
DEVICES = [
    {"index": 0, "name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"index": 1, "name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
    {"index": 2, "name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
    {"index": 3, "name": "BlackHole 16ch", "max_input_channels": 16, "max_output_channels": 16},
]


def ev(t, **kw):
    return json.dumps({"type": t, **kw})


HAPPY = [
    ev("session.updated"),
    ev("input_audio_buffer.speech_started"),
    ev("conversation.item.input_audio_transcription.completed", transcript="Hello?"),
    ev("response.output_audio.delta", delta=base64.b64encode(b"\x01\x00" * 240).decode()),
    ev("response.output_audio_transcript.done", transcript="Hi, this is Ana's assistant."),
    ev("response.audio.delta", delta=base64.b64encode(b"\x02\x00" * 240).decode()),  # old (beta) event name
    ev("response.function_call_arguments.done", name="end_call", call_id="c1",
       arguments=json.dumps({"summary": "Confirmed the meeting at 3pm.", "goal_met": True})),
]


class FakeWS:
    def __init__(self, events, hang=False):
        self.events = list(events)
        self.sent: list[dict] = []
        self.hang = hang
        self.url = None
        self.key = None

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def close(self):
        self._closed = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self.events:
            await asyncio.sleep(0)
            yield e
        if self.hang:
            while not getattr(self, "_closed", False):
                await asyncio.sleep(0.01)


class FakeBridge(voice.AudioBridge):
    def __init__(self, **kw):
        self.kw = kw
        self.played = b""
        self.cleared = 0
        self.stopped = False

    def start(self, on_input):
        on_input(b"\x05\x00" * 480)

    def play(self, pcm):
        self.played += pcm

    def clear(self):
        self.cleared += 1

    def pending_seconds(self):
        return 0.0

    def stop(self):
        self.stopped = True


class StepClock:
    def __init__(self, step=0.0):
        self.t = 1000.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def deps_for(tmp_path, ws, ui=None, devices=DEVICES, key=SECRET, clock=None, bridges=None):
    bridges = bridges if bridges is not None else []

    async def connect(url, k):
        ws.url, ws.key = url, k
        return ws

    def make_bridge(**kw):
        b = FakeBridge(**kw)
        bridges.append(b)
        return b

    return VoiceDeps(
        devices=lambda: devices,
        key=lambda env: key,
        list_models=lambda k: ["gpt-4o", "gpt-realtime", "gpt-realtime-mini"],
        make_bridge=make_bridge,
        connect=connect,
        ui=ui,
        save_dir=tmp_path / "calls",
        clock=clock or StepClock(),
    )


# ------------------------------------------------------------ pieces


def test_instructions():
    txt = voice.build_instructions("Confirm tomorrow's meeting", 5, owner="Ana")
    assert "Ana's assistant" in txt
    assert "NEVER say you are Ana" in txt and "never imitate their voice" in txt
    neutral = voice.build_instructions("Confirm tomorrow's meeting", 5)
    assert "a virtual assistant" in neutral and "Ana" not in neutral
    assert "Confirm tomorrow's meeting" in txt and "will get back" in txt
    assert "end_call" in txt and "5 minute" in txt
    assert "Start speaking in English" in txt and "switch to their language" in txt
    upd = voice.session_update(txt)
    assert upd["session"]["tools"][0]["name"] == "end_call"
    assert upd["session"]["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert "language" not in upd["session"]["audio"]["input"]["transcription"]


def test_instructions_with_fixed_language():
    txt = voice.build_instructions("Confirm the meeting", 5, language="pt-BR")
    assert "Speak only pt-BR" in txt and "Start speaking in English" not in txt
    upd = voice.session_update(txt, language="pt-BR")
    assert upd["session"]["audio"]["input"]["transcription"]["language"] == "pt"


def test_voice_language_from_env():
    assert voice.voice_language({"WA_VOICE_LANGUAGE": " pt-BR "}) == "pt-BR"
    assert voice.voice_language({}) is None


def test_owner_name_from_env():
    assert voice.owner_name({"WA_OWNER_NAME": "  Ana  "}) == "Ana"
    assert voice.owner_name({"WA_OWNER_NAME": ""}) is None
    assert voice.owner_name({}) is None


def test_choose_model():
    lm = lambda k: ["gpt-realtime", "gpt-4o"]  # noqa: E731
    assert voice.choose_model({"WA_REALTIME_MODEL": "x-model"}, SECRET, lm)["model"] == "x-model"
    assert voice.choose_model({}, SECRET, lm) == {"model": "gpt-realtime", "source": "/v1/models"}
    other = voice.choose_model({}, SECRET, lambda k: ["gpt-4o-realtime-preview-2025-06-03", "gpt-realtime-2026-01-01", "gpt-realtime-mini"])
    assert other["model"] == "gpt-realtime-2026-01-01"
    assert voice.choose_model({}, None, lm)["model"] == "gpt-realtime"

    def boom(k):
        raise OSError("rede")

    assert voice.choose_model({}, SECRET, boom)["model"] == "gpt-realtime"


def test_get_api_key_sources():
    assert voice.get_api_key({"OPENAI_API_KEY": SECRET}) == SECRET
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=SECRET + "\n", stderr="")

    assert voice.get_api_key({}, runner) == SECRET
    assert calls[0] == ["security", "find-generic-password", "-s", "wa-mac-openai", "-w"]
    none = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found")  # noqa: E731
    assert voice.get_api_key({}, none) is None


def test_check_blackhole():
    assert voice.check_blackhole(DEVICES) == {"input": 2, "output": 3}
    with pytest.raises(MissingDependency) as exc:
        voice.check_blackhole(DEVICES[:3])
    assert "brew install --cask blackhole-2ch blackhole-16ch" in exc.value.message
    assert exc.value.details["missing"] == ["BlackHole 16ch"]


def test_resample():
    import numpy as np

    pcm = (np.sin(np.arange(4800) / 10) * 10000).astype("<i2").tobytes()
    out = voice.resample_pcm16(pcm, 48000, 24000)
    assert len(out) == len(pcm) // 2
    assert voice.resample_pcm16(pcm, 24000, 24000) == pcm
    assert voice.to_mono_pcm16(np.zeros((10, 2), dtype=np.float32), 2) == b"\x00\x00" * 10


# ------------------------------------------------------------ session


def test_session_happy_path():
    ws = FakeWS(HAPPY)
    bridge = FakeBridge()

    async def connect(url, key):
        ws.url, ws.key = url, key
        return ws

    s = VoiceSession("Confirm the meeting", 5, "gpt-realtime", SECRET, bridge, connect=connect, clock=StepClock())
    res = asyncio.run(s.run())
    assert ws.url.endswith("model=gpt-realtime") and ws.key == SECRET
    assert ws.sent[0]["type"] == "session.update"
    assert any(m["type"] == "input_audio_buffer.append" for m in ws.sent)
    out = [m for m in ws.sent if m["type"] == "conversation.item.create"]
    assert out[-1]["item"] == {"type": "function_call_output", "call_id": "c1", "output": "ok"}
    assert bridge.played == b"\x01\x00" * 240 + b"\x02\x00" * 240 and bridge.cleared == 1 and bridge.stopped
    assert res["ended_by"] == "agent"
    assert res["outcome"] == {"summary": "Confirmed the meeting at 3pm.", "goal_met": True}
    assert [t["role"] for t in res["transcript"]] == ["person", "assistant"]


def test_session_max_minutes():
    ws = FakeWS([ev("session.updated")], hang=True)
    bridge = FakeBridge()

    async def connect(url, key):
        return ws

    s = VoiceSession("x", 1, "m", SECRET, bridge, connect=connect, clock=StepClock(step=5))
    res = asyncio.run(s.run())
    assert res["ended_by"] == "max_minutes"
    wrap = [m for m in ws.sent if m["type"] == "conversation.item.create" and m["item"].get("role") == "system"]
    assert wrap and "say goodbye" in wrap[0]["item"]["content"][0]["text"]
    assert any(m["type"] == "response.create" for m in ws.sent)


def test_session_closed_by_server():
    ws = FakeWS([ev("error", error={"message": "failed"})])

    async def connect(url, key):
        return ws

    res = asyncio.run(VoiceSession("x", 1, "m", SECRET, FakeBridge(), connect=connect, clock=StepClock()).run())
    assert res["ended_by"] == "connection_closed" and res["errors"] == ["failed"]


# ------------------------------------------------------------ orchestration


def test_voice_call_plan_and_locks(wa, tmp_path):
    ws = FakeWS(HAPPY)
    d = deps_for(tmp_path, ws)
    out = voice.voice_call(wa, "bruno", "Confirmar", env=CALLS, deps=d)
    assert out["mode"] == "plan" and ws.sent == []
    with pytest.raises(ActionRefused):
        voice.voice_call(wa, "bruno", "Confirmar", confirm=True, env={}, deps=d)
    with pytest.raises(InvalidArgument):
        voice.voice_call(wa, "bruno", "", env=CALLS, deps=d)
    with pytest.raises(InvalidArgument):
        voice.voice_call(wa, "bruno", "x", max_minutes=120, env=CALLS, deps=d)


def test_voice_call_requires_blackhole_and_key_before_calling(wa, data_dir, tmp_path):
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    ui = make_ui(fake, FakeClock())
    with pytest.raises(MissingDependency):
        voice.voice_call(wa, "bruno", "x", confirm=True, env=CALLS, deps=deps_for(tmp_path, FakeWS([]), ui=ui, devices=DEVICES[:2]))
    with pytest.raises(MissingDependency) as exc:
        voice.voice_call(wa, "bruno", "x", confirm=True, env=CALLS, deps=deps_for(tmp_path, FakeWS([]), ui=ui, key=None))
    assert "wa-mac-openai" in exc.value.message
    assert fake.calls == []


def test_voice_call_full_flow(wa, data_dir, tmp_path):
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    ui = make_ui(fake, FakeClock())
    ws = FakeWS(HAPPY)
    bridges = []
    out = voice.voice_call(wa, "bruno", "Confirm the meeting", 2, monitor=True, confirm=True, env=CALLS,
                           deps=deps_for(tmp_path, ws, ui=ui, bridges=bridges))
    assert out["called"] is True and out["ended_by"] == "agent"
    assert out["summary"] == "Confirmed the meeting at 3pm."
    assert fake.clicked == ["Ligação de voz", "BlackHole 16ch", "BlackHole 2ch", "Encerrar ligação"]
    assert fake.mic == "BlackHole 16ch" and fake.out == "BlackHole 2ch" and not fake.in_call
    assert bridges[0].kw == {"input_device": 2, "output_device": 3, "monitor": True}
    assert out["hangup"]["done"] is True
    saved = json.loads(open(out["record_path"]).read())
    assert saved["goal"] == "Confirm the meeting" and len(saved["transcript"]) == 2
    assert SECRET not in open(out["record_path"]).read()
    assert SECRET not in json.dumps(out)


def test_voice_call_session_error_still_hangs_up(wa, data_dir, tmp_path):
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    ui = make_ui(fake, FakeClock())
    d = deps_for(tmp_path, FakeWS([]), ui=ui)

    async def broken(url, key):
        raise OSError("no network")

    d.connect = broken
    out = voice.voice_call(wa, "bruno", "x", confirm=True, env=CALLS, deps=d)
    assert out["error"]["error"] == "voice_session_failed"
    assert fake.clicked[-1] == "Encerrar ligação" and not fake.in_call


def test_voice_call_not_started(wa, data_dir, tmp_path):
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES, front="Terminal")
    ui = make_ui(fake, FakeClock())
    ws = FakeWS(HAPPY)
    out = voice.voice_call(wa, "bruno", "x", confirm=True, env=CALLS, deps=deps_for(tmp_path, ws, ui=ui))
    assert out["called"] is False and out["ended_by"] == "call_not_started"
    assert fake.clicked == [] and ws.sent == []


def test_voice_test_mode(tmp_path):
    ws = FakeWS(HAPPY)
    bridges = []
    out = voice.voice_test("Practice", 1, env={}, deps=deps_for(tmp_path, ws, bridges=bridges))
    assert out["mode"] == "test" and out["ended_by"] == "agent"
    assert bridges[0].kw == {"input_device": None, "output_device": None, "monitor": False}


# ------------------------------------------------------------ CLI and MCP


def test_cli_and_mcp_voice(data_dir, tmp_path, capsys, monkeypatch):
    fake = FakeWhatsApp(data_dir / "ChatStorage.sqlite", PHONES)
    ui = make_ui(fake, FakeClock())
    monkeypatch.setattr(cli, "_voice_deps", lambda: deps_for(tmp_path, FakeWS(HAPPY), ui=ui))
    monkeypatch.setattr(mcp_server, "_voice_deps", lambda: deps_for(tmp_path, FakeWS(HAPPY), ui=ui))
    rc = cli.run(["voice-call", "--to", "bruno", "--goal", "x"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["mode"] == "plan"
    rc = cli.run(["voice-call", "--to", "bruno", "--goal", "x", "--confirm"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "action_refused"
    assert mcp_server.wa_voice_call("bruno", "x", confirm=True)["error"] == "action_refused"
    monkeypatch.setenv("WA_ALLOW_CALLS", "1")
    res = mcp_server.wa_voice_call("bruno", "x", max_minutes=1, confirm=True)
    assert res["called"] is True and res["summary"]
    assert fake.clicked[0] == "Ligação de voz"
