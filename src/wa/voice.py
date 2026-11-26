"""Voice agent: an audio bridge between a WhatsApp call and the OpenAI Realtime API.

Audio (two BlackHole virtual cables):
- WhatsApp > Speaker    = BlackHole 2ch  -> the agent listens here (input);
- WhatsApp > Microphone = BlackHole 16ch -> the agent speaks here (output).

The agent introduces itself as the assistant of whoever uses `wa` (name in
WA_OWNER_NAME; without it, as "a virtual assistant"), follows the goal, has the
`end_call` tool and respects the time limit. The spoken language comes from
WA_VOICE_LANGUAGE (e.g. `pt-BR`); by default it starts in English and switches to
the other person's language. The transcript goes to
~/.local/share/wa-mac/calls/<date>.json (outside the repository).

The OpenAI key comes from the Keychain (service "wa-mac-openai") or OPENAI_API_KEY
and is never logged or stored.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .db import ActionRefused, InvalidArgument, MissingDependency, WAError

KEYCHAIN_SERVICE = "wa-mac-openai"
DEFAULT_MODEL = "gpt-realtime"
DEFAULT_VOICE = "marin"
REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"
MODELS_URL = "https://api.openai.com/v1/models"
API_RATE = 24_000  # PCM16 mono 24 kHz
DEV_IN = "BlackHole 2ch"  # WhatsApp output -> agent
DEV_OUT = "BlackHole 16ch"  # agent -> WhatsApp microphone
BREW_CMD = "brew install --cask blackhole-2ch blackhole-16ch"
MAX_MINUTES_LIMIT = 30
WRAP_UP_SECONDS = 20  # before the end, ask the agent to say goodbye
HARD_STOP_GRACE = 15
DRAIN_TIMEOUT = 6.0
MAX_GOAL_CHARS = 1000


def owner_name(env: Mapping[str, str] | None = None) -> str | None:
    """Name of the person the agent represents (WA_OWNER_NAME); None when unset."""
    env = os.environ if env is None else env
    name = (env.get("WA_OWNER_NAME") or "").strip()
    return name or None


def voice_language(env: Mapping[str, str] | None = None) -> str | None:
    """Language the agent must speak (WA_VOICE_LANGUAGE, e.g. `pt-BR`); None = follow the other person."""
    env = os.environ if env is None else env
    lang = (env.get("WA_VOICE_LANGUAGE") or "").strip()
    return lang or None


def calls_dir() -> Path:
    base = os.environ.get("WA_CALLS_DIR")
    return Path(base).expanduser() if base else Path.home() / ".local" / "share" / "wa-mac" / "calls"


# ------------------------------------------------------------ key and model


def get_api_key(env: Mapping[str, str] | None = None, runner=subprocess.run) -> str | None:
    env = os.environ if env is None else env
    key = env.get("OPENAI_API_KEY")
    if key:
        return key.strip()
    try:
        proc = runner(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return proc.stdout.strip()
    return None


def missing_key_error() -> MissingDependency:
    return MissingDependency(
        "OpenAI key not found. Store it in the Keychain with: "
        f'security add-generic-password -s {KEYCHAIN_SERVICE} -a "$USER" -w '
        "(the command prompts for the key), or set OPENAI_API_KEY in the environment."
    )


def _http_models(key: str) -> list[str]:
    req = urllib.request.Request(MODELS_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed https URL)
        data = json.load(resp)
    return [m.get("id", "") for m in data.get("data", [])]


def choose_model(env: Mapping[str, str], key: str | None, list_models: Callable[[str], list[str]] = _http_models) -> dict:
    """WA_REALTIME_MODEL wins. Otherwise the default, confirmed against /v1/models when there is a key."""
    forced = env.get("WA_REALTIME_MODEL")
    if forced:
        return {"model": forced, "source": "WA_REALTIME_MODEL"}
    if not key:
        return {"model": DEFAULT_MODEL, "source": "default (no key to confirm it)"}
    try:
        ids = list_models(key)
    except Exception as exc:  # network/HTTP: keep the default
        return {"model": DEFAULT_MODEL, "source": f"default (not confirmed: {type(exc).__name__})"}
    if DEFAULT_MODEL in ids:
        return {"model": DEFAULT_MODEL, "source": "/v1/models"}
    realtime = sorted(i for i in ids if "realtime" in i and "mini" not in i)
    realtime = realtime or sorted(i for i in ids if "realtime" in i)
    if realtime:
        stable = [i for i in realtime if "preview" not in i]
        return {"model": (stable or realtime)[-1], "source": "/v1/models (default unavailable)"}
    return {"model": DEFAULT_MODEL, "source": "default (no realtime model listed)"}


# ------------------------------------------------------------ devices


def list_audio_devices() -> list[dict]:
    import sounddevice as sd

    return [dict(d) for d in sd.query_devices()]


def find_device(devices: list[dict], fragment: str, kind: str) -> int | None:
    frag = fragment.casefold()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for idx, d in enumerate(devices):
        if frag in str(d.get("name", "")).casefold() and d.get(key, 0) > 0:
            return d.get("index", idx)
    return None


def check_blackhole(devices: list[dict]) -> dict:
    have_in = find_device(devices, DEV_IN, "input")
    have_out = find_device(devices, DEV_OUT, "output")
    missing = [n for n, v in ((DEV_IN, have_in), (DEV_OUT, have_out)) if v is None]
    if missing:
        raise MissingDependency(
            f"Virtual audio cables missing: {', '.join(missing)}. Install them with: {BREW_CMD} "
            "(then restart the Mac or the audio service: sudo killall coreaudiod).",
            missing=missing,
            install=BREW_CMD,
        )
    return {"input": have_in, "output": have_out}


# ------------------------------------------------------------ resampling


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    import numpy as np

    if not data or src_rate == dst_rate:
        return data
    x = np.frombuffer(data, dtype="<i2").astype(np.float32)
    n_out = max(1, int(round(len(x) * dst_rate / src_rate)))
    t_src = np.arange(len(x)) / src_rate
    t_dst = np.arange(n_out) / dst_rate
    y = np.interp(t_dst, t_src, x)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def to_mono_pcm16(frames, channels: int) -> bytes:
    """float32/int16 (frames x channels) -> PCM16 mono."""
    import numpy as np

    a = np.asarray(frames)
    if a.ndim == 2 and a.shape[1] > 1:
        a = a.mean(axis=1)
    elif a.ndim == 2:
        a = a[:, 0]
    if a.dtype != np.int16:
        a = np.clip(a * 32767.0, -32768, 32767).astype("<i2")
    return a.astype("<i2").tobytes()


# ------------------------------------------------------------ instructions


def build_instructions(goal: str, max_minutes: float, owner: str | None = None, language: str | None = None) -> str:
    if owner:
        who = f"{owner}'s voice assistant"
        intro = f"Hi, this is {owner}'s assistant."
        principal = owner
    else:
        who = "the voice assistant of the person who uses this WhatsApp account"
        intro = "Hi, this is a virtual assistant."
        principal = "the person you represent"
    if language:
        lang_rule = f"Speak only {language}, in short, natural sentences. Say your introduction in {language} too.\n"
    else:
        lang_rule = (
            "Start speaking in English. If the other person answers in another language, switch to "
            "their language and keep using it. Use short, natural sentences.\n"
        )
    return (
        f"You are {who}, on a WhatsApp call on their behalf.\n"
        + lang_rule
        + f"In your FIRST turn, introduce yourself like this: \"{intro}\" "
        "and say in one sentence why you are calling.\n"
        f"You NEVER say you are {principal}, never pretend to be them and never imitate their voice. "
        "If asked, say you are a virtual assistant speaking on their behalf.\n"
        f"Goal of the call (follow only this): {goal}\n"
        f"If the person asks for anything outside the goal, politely say that {principal} will get back "
        "to them, then return to the goal. Do not agree to commitments, amounts or dates that are not in the goal.\n"
        "Do not ask for or write down passwords, codes or bank details.\n"
        f"The call lasts at most {max_minutes:g} minute(s). When the goal is done, when the person wants "
        "to finish or when time runs out, say goodbye in one sentence and call the `end_call` tool with "
        "a summary of the result."
    )


END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": "Ends the call. Call it after saying goodbye.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Result of the call in 1 to 3 sentences."},
            "goal_met": {"type": "boolean"},
        },
        "required": ["summary"],
    },
}


def session_update(instructions: str, voice: str = DEFAULT_VOICE, language: str | None = None) -> dict:
    transcription = {"model": "gpt-4o-mini-transcribe"}
    if language:
        transcription["language"] = language.split("-")[0].lower()  # pt-BR -> pt
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": API_RATE},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": transcription,
                },
                "output": {"format": {"type": "audio/pcm", "rate": API_RATE}, "voice": voice},
            },
            "tools": [END_CALL_TOOL],
            "tool_choice": "auto",
        },
    }


# ------------------------------------------------------------ audio


class AudioBridge:
    """Audio interface used by the session (the real one uses sounddevice; the tests, a fake)."""

    def start(self, on_input: Callable[[bytes], None]) -> None:  # PCM16 mono 24 kHz
        raise NotImplementedError

    def play(self, pcm24k: bytes) -> None:
        raise NotImplementedError

    def clear(self) -> None:  # interruption (the person started talking)
        raise NotImplementedError

    def pending_seconds(self) -> float:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class _Ring:
    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()

    def push(self, data: bytes):
        with self.lock:
            self.buf.extend(data)

    def pull(self, n: int) -> bytes:
        with self.lock:
            out = bytes(self.buf[:n])
            del self.buf[:n]
        return out + b"\0" * (n - len(out))

    def clear(self):
        with self.lock:
            self.buf.clear()

    def __len__(self):
        with self.lock:
            return len(self.buf)


class SoundDeviceBridge(AudioBridge):
    """Device input -> agent; agent -> output. `monitor` mirrors both sides to the
    default speaker."""

    def __init__(self, input_device=None, output_device=None, monitor: bool = False, blocksize_ms: int = 20):
        import sounddevice as sd

        self.sd = sd
        self.input_device = input_device
        self.output_device = output_device
        self.monitor = monitor
        self.blocksize_ms = blocksize_ms
        self.out_ring = _Ring()
        self.mon_ring = _Ring()
        self.streams: list = []
        self.out_rate = API_RATE

    def _rate(self, device, kind) -> int:
        info = self.sd.query_devices(device, kind)
        return int(info["default_samplerate"])

    def start(self, on_input):
        sd = self.sd
        in_rate = self._rate(self.input_device, "input")
        in_ch = 1
        self.out_rate = self._rate(self.output_device, "output")

        def in_cb(indata, frames, t, status):
            pcm = to_mono_pcm16(indata, in_ch)
            pcm24 = resample_pcm16(pcm, in_rate, API_RATE)
            on_input(pcm24)
            if self.monitor:
                self.mon_ring.push(pcm24)

        def out_cb(outdata, frames, t, status):
            outdata[:] = _bytes_to_frames(self.out_ring.pull(frames * 2), outdata.shape)

        self.streams.append(sd.InputStream(device=self.input_device, channels=in_ch, samplerate=in_rate, dtype="int16",
                                           blocksize=int(in_rate * self.blocksize_ms / 1000), callback=in_cb))
        self.streams.append(sd.OutputStream(device=self.output_device, channels=1, samplerate=self.out_rate, dtype="int16",
                                            blocksize=int(self.out_rate * self.blocksize_ms / 1000), callback=out_cb))
        if self.monitor:
            mon_rate = self._rate(None, "output")
            self.mon_rate = mon_rate

            def mon_cb(outdata, frames, t, status):
                outdata[:] = _bytes_to_frames(self.mon_ring.pull(frames * 2), outdata.shape)

            self.streams.append(sd.OutputStream(device=None, channels=1, samplerate=mon_rate, dtype="int16",
                                                blocksize=int(mon_rate * self.blocksize_ms / 1000), callback=mon_cb))
        for s in self.streams:
            s.start()

    def play(self, pcm24k):
        self.out_ring.push(resample_pcm16(pcm24k, API_RATE, self.out_rate))
        if self.monitor:
            self.mon_ring.push(resample_pcm16(pcm24k, API_RATE, getattr(self, "mon_rate", API_RATE)))

    def clear(self):
        self.out_ring.clear()

    def pending_seconds(self):
        return len(self.out_ring) / 2 / self.out_rate

    def stop(self):
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams.clear()


def _bytes_to_frames(data: bytes, shape):
    import numpy as np

    return np.frombuffer(data, dtype="<i2").reshape(shape)


# ------------------------------------------------------------ Realtime session


async def _ws_connect(url: str, key: str):
    from websockets.asyncio.client import connect

    return await connect(url, additional_headers={"Authorization": f"Bearer {key}"}, max_size=None)


AUDIO_DELTA = ("response.output_audio.delta", "response.audio.delta")
AGENT_TRANSCRIPT_DONE = ("response.output_audio_transcript.done", "response.audio_transcript.done")
USER_TRANSCRIPT_DONE = ("conversation.item.input_audio_transcription.completed",)


@dataclass
class VoiceSession:
    goal: str
    max_minutes: float
    model: str
    key: str
    audio: AudioBridge
    connect: Callable[[str, str], Awaitable[Any]] = _ws_connect
    clock: Callable[[], float] = time.monotonic
    owner: str | None = None
    language: str | None = None
    voice: str = DEFAULT_VOICE
    transcript: list[dict] = field(default_factory=list)
    outcome: dict = field(default_factory=dict)
    ended_by: str | None = None
    errors: list[str] = field(default_factory=list)

    async def run(self) -> dict:
        started = self.clock()
        deadline = started + self.max_minutes * 60
        ws = await self.connect(REALTIME_URL.format(model=self.model), self.key)
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        wrap_sent = False

        async def send(obj):
            await ws.send(json.dumps(obj))

        def on_input(pcm: bytes):
            if stop.is_set() or not pcm:
                return
            msg = json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_safe_send(ws, msg)))

        await send(session_update(build_instructions(self.goal, self.max_minutes, self.owner, self.language),
                                  self.voice, self.language))
        self.audio.start(on_input)

        async def reader():
            nonlocal wrap_sent
            async for raw in ws:
                ev = json.loads(raw)
                t = ev.get("type", "")
                if t in AUDIO_DELTA:
                    self.audio.play(base64.b64decode(ev.get("delta", "")))
                elif t == "input_audio_buffer.speech_started":
                    self.audio.clear()  # the person interrupted
                elif t in AGENT_TRANSCRIPT_DONE:
                    self._add("assistant", ev.get("transcript", ""), started)
                elif t in USER_TRANSCRIPT_DONE:
                    self._add("person", ev.get("transcript", ""), started)
                elif t == "response.function_call_arguments.done" and ev.get("name") == "end_call":
                    try:
                        args = json.loads(ev.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    self.outcome = {"summary": args.get("summary"), "goal_met": args.get("goal_met")}
                    await send({"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": ev.get("call_id"), "output": "ok"}})
                    self.ended_by = self.ended_by or "agent"
                    stop.set()
                    return
                elif t == "error":
                    msg = (ev.get("error") or {}).get("message", "error")
                    self.errors.append(str(msg)[:300])
            self.ended_by = self.ended_by or "connection_closed"
            stop.set()

        async def timer():
            nonlocal wrap_sent
            while not stop.is_set():
                now = self.clock()
                if not wrap_sent and now >= deadline - WRAP_UP_SECONDS:
                    wrap_sent = True
                    await send({"type": "conversation.item.create", "item": {
                        "type": "message", "role": "system",
                        "content": [{"type": "input_text", "text": "Time is almost up: say goodbye now and call end_call."}]}})
                    await send({"type": "response.create"})
                if now >= deadline + HARD_STOP_GRACE:
                    self.ended_by = self.ended_by or "max_minutes"
                    stop.set()
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

        tasks = [asyncio.ensure_future(reader()), asyncio.ensure_future(timer())]
        try:
            await stop.wait()
            # let the goodbye finish playing
            drain_until = self.clock() + DRAIN_TIMEOUT
            while self.audio.pending_seconds() > 0.05 and self.clock() < drain_until:
                await asyncio.sleep(0.1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.ended_by = self.ended_by or "interrupted"
        finally:
            for tsk in tasks:
                tsk.cancel()
            self.audio.stop()
            try:
                await ws.close()
            except Exception:
                pass
        return {
            "ended_by": self.ended_by,
            "duration_s": round(self.clock() - started, 1),
            "outcome": self.outcome,
            "transcript": self.transcript,
            "errors": self.errors,
        }

    def _add(self, role: str, text: str, started: float):
        text = (text or "").strip()
        if text:
            self.transcript.append({"role": role, "t": round(self.clock() - started, 1), "text": text})


async def _safe_send(ws, msg: str):
    try:
        await ws.send(msg)
    except Exception:
        pass


def save_record(record: dict, directory: Path | None = None) -> Path:
    directory = directory or calls_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}.json"
    n = 1
    while path.exists():
        path = directory / f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}-{n}.json"
        n += 1
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _summary(result: dict) -> str:
    out = result.get("outcome") or {}
    if out.get("summary"):
        return out["summary"]
    return f"Call ended ({result.get('ended_by')}); the agent left no summary."


# ------------------------------------------------------------ orchestration


def _validate(goal: str, max_minutes: float) -> tuple[str, float]:
    goal = (goal or "").strip()
    if not goal:
        raise InvalidArgument("give the goal of the call (--goal)")
    if len(goal) > MAX_GOAL_CHARS:
        raise InvalidArgument(f"goal too long (maximum {MAX_GOAL_CHARS} characters)")
    try:
        max_minutes = float(max_minutes)
    except (TypeError, ValueError):
        raise InvalidArgument("max-minutes must be a number") from None
    if not 0.5 <= max_minutes <= MAX_MINUTES_LIMIT:
        raise InvalidArgument(f"max-minutes must be between 0.5 and {MAX_MINUTES_LIMIT}")
    return goal, max_minutes


@dataclass
class VoiceDeps:
    """Everything that touches the real world, so tests can swap it."""

    devices: Callable[[], list[dict]] = list_audio_devices
    key: Callable[[Mapping[str, str]], str | None] = get_api_key
    list_models: Callable[[str], list[str]] = _http_models
    make_bridge: Callable[..., AudioBridge] = SoundDeviceBridge
    connect: Callable[[str, str], Awaitable[Any]] = _ws_connect
    ui: Any = None
    save_dir: Path | None = None
    clock: Callable[[], float] = time.monotonic


def voice_call(wa, to: str, goal: str, max_minutes: float = 5, monitor: bool = False, confirm: bool = False,
               env: Mapping[str, str] | None = None, deps: VoiceDeps | None = None) -> dict:
    from . import actions

    env = os.environ if env is None else env
    deps = deps or VoiceDeps()
    goal, max_minutes = _validate(goal, max_minutes)
    rec, _ = actions._recipient(wa, to)
    plan = {
        "mode": "plan",
        "to": rec,
        "goal": goal,
        "max_minutes": max_minutes,
        "monitor": monitor,
        "plan": [
            f"call {rec['phone']} (menu Chat > Voice call)",
            f"Call menu: Microphone = {DEV_OUT}, Speaker = {DEV_IN}",
            "audio bridge with the OpenAI Realtime API",
            "hang up through Call > End Call",
            f"save the transcript to {calls_dir()}",
        ],
        "allowed_by_env": actions.calls_allowed(env),
    }
    if not confirm:
        plan["hint"] = "Nothing was done. To call: --confirm (MCP: confirm=true) with WA_ALLOW_CALLS=1."
        return plan
    if not actions.calls_allowed(env):
        raise ActionRefused("Call refused: set WA_ALLOW_CALLS=1 in the process environment.", plan=plan)

    devices = deps.devices()
    idx = check_blackhole(devices)
    key = deps.key(env)
    if not key:
        raise missing_key_error()
    model = choose_model(env, key, deps.list_models)

    ui = deps.ui or actions.WhatsAppUI()
    call = actions.start_call(wa, to, "voice", confirm=True, env=env, ui=ui)
    record: dict = {"kind": "whatsapp_call", "to": rec, "goal": goal, "model": model["model"],
                    "max_minutes": max_minutes, "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "call": {k: call.get(k) for k in ("started", "verified", "reason")}}
    try:
        if not call.get("started"):
            record["ended_by"] = "call_not_started"
        else:
            record["devices"] = {
                "microphone": actions.select_call_device(ui, actions.SUBMENU_MICROPHONE, DEV_OUT),
                "speaker": actions.select_call_device(ui, actions.SUBMENU_SPEAKER, DEV_IN),
            }
            bridge = deps.make_bridge(input_device=idx["input"], output_device=idx["output"], monitor=monitor)
            session = VoiceSession(goal, max_minutes, model["model"], key, bridge, connect=deps.connect, clock=deps.clock,
                                   owner=owner_name(env), language=voice_language(env))
            record.update(asyncio.run(session.run()))
    except WAError as exc:
        record["error"] = exc.to_dict()
        record.setdefault("ended_by", "error")
    except Exception as exc:  # network, audio: hang up anyway
        record["error"] = {"error": "voice_session_failed", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
        record.setdefault("ended_by", "error")
    finally:
        if call.get("started"):
            try:
                hung = actions.hangup(confirm=True, env=env, ui=ui)
                record["hangup"] = {k: hung.get(k) for k in ("done", "verified", "reason")}
            except WAError as exc:
                record["hangup"] = exc.to_dict()
    record["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return _finish(record, deps, {"mode": "executed", "called": bool(call.get("started"))})


def _finish(record: dict, deps: VoiceDeps, out: dict) -> dict:
    path = save_record(record, deps.save_dir)
    out.update(
        {
            "summary": _summary(record),
            "ended_by": record.get("ended_by"),
            "duration_s": record.get("duration_s"),
            "outcome": record.get("outcome"),
            "turns": len(record.get("transcript") or []),
            "record_path": str(path),
        }
    )
    for k in ("error", "hangup", "devices", "model"):
        if k in record:
            out[k] = record[k]
    return out


def voice_test(goal: str, max_minutes: float = 3, env: Mapping[str, str] | None = None,
               deps: VoiceDeps | None = None) -> dict:
    """Same session, with the Mac's default microphone and speaker, without WhatsApp."""
    env = os.environ if env is None else env
    deps = deps or VoiceDeps()
    goal, max_minutes = _validate(goal, max_minutes)
    key = deps.key(env)
    if not key:
        raise missing_key_error()
    model = choose_model(env, key, deps.list_models)
    bridge = deps.make_bridge(input_device=None, output_device=None, monitor=False)
    session = VoiceSession(goal, max_minutes, model["model"], key, bridge, connect=deps.connect, clock=deps.clock,
                           owner=owner_name(env), language=voice_language(env))
    record = {"kind": "voice_test", "goal": goal, "model": model["model"], "max_minutes": max_minutes,
              "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    record.update(asyncio.run(session.run()))
    record["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return _finish(record, deps, {"mode": "test", "model": model})
