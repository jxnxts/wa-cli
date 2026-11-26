"""Speech to text for voice notes, audio and the sound of videos.

Local first, in this order:
1. mlx-whisper (`mlx_whisper` on PATH or in ~/.local/bin; Apple Silicon);
2. whisper.cpp (`whisper-cli`), with a ggml model in WA_WHISPER_CPP_MODEL.

The OpenAI API is used only with an explicit opt-in: WA_TRANSCRIBE_REMOTE=1 and the
key in the Keychain (service `wa-mac-openai`) or OPENAI_API_KEY. Then the audio leaves
the Mac.

Settings: WA_TRANSCRIBE_ENGINE (auto, mlx, whisper.cpp, openai), WA_TRANSCRIBE_MODEL
(a Hugging Face repo for mlx, a ggml file for whisper.cpp, a model name for OpenAI) and
WA_TRANSCRIBE_LANGUAGE (an ISO code such as pt or en; empty means detect it).
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest

from .db import InvalidArgument, TranscriberUnavailable, WAError
from .extract import Tools, to_wav16k

ENGINES = ("auto", "mlx", "whisper.cpp", "openai")
MLX_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
OPENAI_DEFAULT_MODEL = "whisper-1"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 1800

INSTALL_HINT = (
    "Install a local transcriber: `uv tool install mlx-whisper` (Apple Silicon), or `brew install whisper-cpp` "
    "and point WA_WHISPER_CPP_MODEL at a ggml model. To use the OpenAI API instead (the audio leaves the Mac), "
    "set WA_TRANSCRIBE_REMOTE=1 and store the key with `security add-generic-password -s wa-mac-openai "
    "-a \"$USER\" -w`."
)


class TranscriptionFailed(WAError):
    code = "transcription_failed"


def settings(env: Mapping[str, str]) -> dict:
    engine = (env.get("WA_TRANSCRIBE_ENGINE") or "auto").strip().lower()
    if engine == "whispercpp":
        engine = "whisper.cpp"
    if engine not in ENGINES:
        raise InvalidArgument(f"WA_TRANSCRIBE_ENGINE must be one of {', '.join(ENGINES)}")
    lang = (env.get("WA_TRANSCRIBE_LANGUAGE") or "").strip().lower() or None
    if lang in ("auto", "none"):
        lang = None
    return {"engine": engine, "model": (env.get("WA_TRANSCRIBE_MODEL") or "").strip() or None, "language": lang,
            "remote_allowed": env.get("WA_TRANSCRIBE_REMOTE") == "1"}


def _whisper_cpp_model(env: Mapping[str, str], model: str | None) -> str | None:
    for cand in (env.get("WA_WHISPER_CPP_MODEL"), model):
        if cand and Path(cand).expanduser().is_file():
            return str(Path(cand).expanduser())
    for folder in (Path.home() / ".local" / "share" / "wa-mac" / "models", Path("/opt/homebrew/share/whisper-cpp"),
                   Path("/usr/local/share/whisper-cpp")):
        if folder.is_dir():
            found = sorted(folder.glob("ggml-*.bin"))
            if found:
                return str(found[-1])
    return None


def pick_engine(tools: Tools, cfg: dict) -> tuple[str, dict]:
    """(engine, details) or TranscriberUnavailable."""
    wanted = cfg["engine"]
    tried = []
    if wanted in ("auto", "mlx"):
        exe = tools.find("mlx_whisper")
        if exe:
            return "mlx", {"exe": exe, "model": cfg["model"] or MLX_DEFAULT_MODEL}
        tried.append("mlx_whisper")
    if wanted in ("auto", "whisper.cpp"):
        exe = tools.find("whisper-cli") or tools.find("whisper-cpp")
        model = _whisper_cpp_model(tools.env, cfg["model"])
        if exe and model:
            return "whisper.cpp", {"exe": exe, "model": model}
        tried.append("whisper-cli (no ggml model; set WA_WHISPER_CPP_MODEL)" if exe else "whisper-cli")
    if wanted in ("auto", "openai"):
        if cfg["remote_allowed"]:
            from .voice import get_api_key

            key = get_api_key(tools.env, runner=tools.runner)
            if key:
                model = cfg["model"] if wanted == "openai" and cfg["model"] else OPENAI_DEFAULT_MODEL
                return "openai", {"key": key, "model": model}
            tried.append("openai (no key in the Keychain wa-mac-openai or OPENAI_API_KEY)")
        elif wanted == "openai":
            tried.append("openai (needs WA_TRANSCRIBE_REMOTE=1)")
    raise TranscriberUnavailable("No transcriber available. " + INSTALL_HINT, tried=tried, install=INSTALL_HINT)


def _mlx(path: Path, d: dict, lang: str | None, tools: Tools, tmp: Path) -> dict:
    cmd = [d["exe"], str(path), "--model", d["model"], "--output-format", "json", "--output-dir", str(tmp),
           "--output-name", "transcript", "--verbose", "False"]
    if lang:
        cmd += ["--language", lang]
    proc = tools.run(cmd, timeout=TIMEOUT)
    out = tmp / "transcript.json"
    if proc.returncode != 0 or not out.exists():
        raise TranscriptionFailed("mlx_whisper failed", detail=((proc.stderr or proc.stdout or "")[-400:]))
    data = json.loads(out.read_text(encoding="utf-8"))
    return {"text": (data.get("text") or "").strip(), "language": data.get("language") or lang}


def _whisper_cpp(path: Path, d: dict, lang: str | None, tools: Tools, tmp: Path) -> dict:
    wav = to_wav16k(path, tools, tmp / "audio.wav")
    base = tmp / "transcript"
    proc = tools.run([d["exe"], "-m", d["model"], "-f", str(wav), "-l", lang or "auto", "-otxt", "-of", str(base),
                      "-np"], timeout=TIMEOUT)
    out = base.with_suffix(".txt")
    if proc.returncode != 0 or not out.exists():
        raise TranscriptionFailed("whisper.cpp failed", detail=((proc.stderr or proc.stdout or "")[-400:]))
    return {"text": out.read_text(encoding="utf-8", errors="replace").strip(), "language": lang}


def _multipart(fields: dict[str, str], file_field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----wa" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _openai(path: Path, d: dict, lang: str | None, tools: Tools, tmp: Path,
            opener: Callable = urlrequest.urlopen) -> dict:
    src = path
    if path.stat().st_size > OPENAI_MAX_BYTES or path.suffix.lower() not in (".ogg", ".opus", ".mp3", ".m4a", ".wav",
                                                                          ".mp4", ".webm", ".mpeg", ".mpga"):
        src = to_wav16k(path, tools, tmp / "audio.wav")
    if src.stat().st_size > OPENAI_MAX_BYTES:
        raise TranscriptionFailed("the audio is larger than the 25 MB the OpenAI API accepts")
    fields = {"model": d["model"], "response_format": "json"}
    if lang:
        fields["language"] = lang
    name = src.name if src.suffix.lower() != ".opus" else src.stem + ".ogg"
    body, ctype = _multipart(fields, "file", name, "application/octet-stream", src.read_bytes())
    req = urlrequest.Request(OPENAI_URL, data=body, method="POST",
                             headers={"Authorization": f"Bearer {d['key']}", "Content-Type": ctype})
    try:
        with opener(req, timeout=300) as resp:  # noqa: S310 (fixed https URL)
            data = json.loads(resp.read() or b"{}")
    except urlerror.HTTPError as exc:
        raise TranscriptionFailed(f"OpenAI answered {exc.code}", detail=(exc.read() or b"")[:300].decode("utf-8", "replace")) from None
    except (urlerror.URLError, OSError) as exc:
        raise TranscriptionFailed("could not reach the OpenAI API", detail=str(exc)[:200]) from None
    return {"text": (data.get("text") or "").strip(), "language": data.get("language") or lang}


def transcribe(path: Path, file_hash: str, tools: Tools, cache, opener: Callable = urlrequest.urlopen) -> dict:
    """{text, language, engine, model, local, cached}. Raises TranscriberUnavailable when there is none."""
    cfg = settings(tools.env)
    engine, d = pick_engine(tools, cfg)
    params = {"engine": engine, "model": d["model"], "language": cfg["language"]}
    hit = cache.get(file_hash, "transcript", params)
    if hit is not None:
        return {**hit, "cached": True}
    with tempfile.TemporaryDirectory(prefix="wa-stt-") as tmp:
        if engine == "mlx":
            res = _mlx(path, d, cfg["language"], tools, Path(tmp))
        elif engine == "whisper.cpp":
            res = _whisper_cpp(path, d, cfg["language"], tools, Path(tmp))
        else:
            res = _openai(path, d, cfg["language"], tools, Path(tmp), opener=opener)
    out = {"text": res["text"], "language": res.get("language"), "engine": engine,
           "model": Path(d["model"]).name if engine == "whisper.cpp" else d["model"], "local": engine != "openai"}
    cache.put(file_hash, "transcript", params, out)
    return {**out, "cached": False}
