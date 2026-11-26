"""Sending files (image, video, audio, voice note, document, sticker) through the companion.

The guards are the same as for text: without confirm you only get the draft; a real
send needs confirm AND WA_ALLOW_SEND=1, and counts against the companion's send limit.
The Desktop app UI cannot send files, so `via desktop` answers `unsupported`.

Which files may go out: a prompt-injected agent could try to send keys or documents
from this Mac. So by default only files inside the outbox folder are accepted
(WA_MEDIA_OUTBOX, default ~/.local/share/wa-mac/outbox, created with mode 700). Paths
with `..` and symbolic links that lead out of the folder are refused. The CLI accepts
other paths with --allow-any-path; the MCP server only with WA_ALLOW_ANY_FILE=1 in its
environment.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping

from . import companion as comp
from .db import FileTooLarge, InvalidArgument, NotFoundError, PathRefused, SendRefused, Unsupported, WAError
from .extract import Tools, to_voice_note
from .send import MAX_SEND_CHARS, send_allowed, validate_text

SEND_KINDS = ("image", "video", "audio", "voice", "document", "sticker")
ROUTES = {
    "image": ("/send/image", "image"),
    "video": ("/send/video", "video"),
    "audio": ("/send/audio", "audio"),
    "voice": ("/send/audio", "audio"),
    "document": ("/send/file", "file"),
    "sticker": ("/send/sticker", "sticker"),
}
CAPTION_KINDS = ("image", "video", "document")
# GoWA's defaults (config/settings.go), used when /app/info does not answer.
DEFAULT_LIMITS = {"max_image_size": 20_000_000, "max_video_size": 100_000_000, "max_file_size": 50_000_000}
EXTRA_MIME = {
    ".opus": "audio/ogg", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif", ".md": "text/markdown", ".mov": "video/quicktime",
}
CONVERT_TO_JPEG = ("image/heic", "image/heif", "image/tiff", "image/bmp")


def outbox_dir(env: Mapping[str, str]) -> Path:
    raw = env.get("WA_MEDIA_OUTBOX")
    return Path(raw).expanduser() if raw else comp.base_dir(env) / "outbox"


def ensure_outbox(env: Mapping[str, str]) -> Path:
    d = outbox_dir(env)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def resolve_file(file: str, env: Mapping[str, str], allow_any: bool) -> Path:
    """The real path of a file that may be sent (see the module notes)."""
    raw = str(file or "").strip()
    if not raw:
        raise InvalidArgument("give the file to send")
    if ".." in Path(raw).parts:
        raise PathRefused("paths with '..' are refused; give the file's full path or its name inside the outbox")
    outbox = ensure_outbox(env).resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = outbox / path
    real = path.resolve()  # follows symbolic links
    if not allow_any:
        try:
            real.relative_to(outbox)
        except ValueError:
            raise PathRefused(
                f"only files inside the outbox folder can be sent ({outbox}). Copy the file there first. "
                "CLI: --allow-any-path sends from anywhere; MCP: only with WA_ALLOW_ANY_FILE=1 in the server "
                "environment.",
                outbox=str(outbox),
            ) from None
    if not real.exists():
        raise NotFoundError(f"file not found: {raw}")
    if not real.is_file():
        raise InvalidArgument(f"not a regular file: {raw}")
    return real


def guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def infer_kind(mime: str) -> str:
    major = mime.split("/", 1)[0]
    if major in ("image", "video", "audio"):
        return major
    return "document"


def check_kind(kind: str, mime: str) -> None:
    major = mime.split("/", 1)[0]
    ok = {
        "image": major == "image",
        "sticker": major == "image",
        "video": major == "video",
        "audio": major == "audio",
        "voice": major in ("audio", "video"),
        "document": True,
    }[kind]
    if not ok:
        raise InvalidArgument(f"a {mime} file cannot be sent as {kind}")


def limits(client: comp.GowaClient | None) -> tuple[dict, str]:
    if client is not None:
        try:
            info = client.results("GET", "/app/info", device=False, timeout=comp.STATUS_TIMEOUT) or {}
            got = {k: int(info[k]) for k in DEFAULT_LIMITS if isinstance(info.get(k), (int, float)) and info[k] > 0}
            if got:
                return {**DEFAULT_LIMITS, **got}, "gowa"
        except (WAError, ValueError, TypeError):
            pass
    return dict(DEFAULT_LIMITS), "default"


def limit_for(kind: str, lim: dict) -> int:
    if kind == "image":
        return lim["max_image_size"]
    if kind == "video":
        return lim["max_video_size"]
    return lim["max_file_size"]


def _connected_client(client_factory: Callable[[], comp.GowaClient]) -> comp.GowaClient | None:
    client = client_factory()
    return client if comp.is_connected(client) else None


def send_media(
    wa,
    to: str,
    file: str,
    caption: str | None = None,
    as_kind: str | None = None,
    confirm: bool = False,
    via: str = "auto",
    reply_to: str | None = None,
    allow_any_path: bool = False,
    max_caption_chars: int = MAX_SEND_CHARS,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[[], comp.GowaClient] | None = None,
    tools: Tools | None = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    env = os.environ if env is None else env
    client_factory = client_factory or (lambda: comp.default_client(env))
    tools = tools or Tools(env=env)
    if via not in comp.VIAS:
        raise InvalidArgument(f"invalid via: {via!r} (use {', '.join(comp.VIAS)})")
    if via == "desktop":
        raise Unsupported(
            "Files can only be sent through the companion (GoWA): the WhatsApp Desktop UI path sends text only. "
            "Use via auto or companion with the companion connected (`wa companion status`). Nothing was sent."
        )
    if as_kind is not None and as_kind not in SEND_KINDS:
        raise InvalidArgument(f"invalid kind: {as_kind!r} (use {', '.join(SEND_KINDS)})")

    path = resolve_file(file, env, allow_any_path)
    mime = guess_mime(path)
    kind = as_kind or infer_kind(mime)
    check_kind(kind, mime)
    if caption is not None and str(caption).strip():
        if kind not in CAPTION_KINDS:
            raise InvalidArgument(f"a {kind} cannot carry a caption (only {', '.join(CAPTION_KINDS)})")
        caption = validate_text(caption, max_caption_chars)
    else:
        caption = None
    target = comp.resolve_target(wa, to)
    client = _connected_client(client_factory)
    lim, lim_source = limits(client)
    size = path.stat().st_size
    limit = limit_for(kind, lim)
    conversion = None
    if kind == "voice":
        conversion = "ffmpeg: ogg/opus mono 48 kHz (voice note)"
    elif kind == "image" and mime in CONVERT_TO_JPEG:
        conversion = "sips: JPEG"
    file_info = {"name": path.name, "path": str(path), "size_bytes": size, "mime": mime, "kind": kind,
                 "kind_inferred": as_kind is None}
    if conversion:
        file_info["conversion"] = conversion

    if not confirm:
        out = {"sent": False, "mode": "draft", "via": "companion", "via_requested": via, "to": target,
               "file": file_info, "caption": caption, "reply_to": reply_to,
               "limit_bytes": limit, "limit_source": lim_source, "within_limit": size <= limit,
               "send_allowed_by_env": send_allowed(env), "rate": comp.check_rate(env, clock()),
               "hint": "Nothing was sent. To really send: confirm=true (CLI: --confirm) with WA_ALLOW_SEND=1."}
        if client is None:
            out["via_note"] = "the companion is not connected right now; sending will fail"
        if size > limit:
            out["note"] = f"The file is larger than the {limit} bytes GoWA accepts for a {kind}."
        return out

    if not send_allowed(env):
        raise SendRefused("Send refused: set WA_ALLOW_SEND=1 in the process environment to allow sending.",
                          draft={"to": target, "file": file_info, "caption": caption, "via": "companion"})
    if client is None:
        raise comp.CompanionUnavailable(
            "Files are sent only through the companion, and it is not connected (`wa companion status`). "
            "Nothing was sent.")
    if size > limit:
        raise FileTooLarge(f"The file has {size} bytes; GoWA accepts up to {limit} for a {kind}. Nothing was sent.",
                           limit_bytes=limit, size_bytes=size)

    with tempfile.TemporaryDirectory(prefix="wa-send-") as tmp:
        upload_path, upload_name, upload_mime = path, path.name, mime.split(";")[0]
        if kind == "voice":
            upload_path = to_voice_note(path, tools, Path(tmp) / "voice.ogg")
            upload_name, upload_mime = "voice.ogg", "audio/ogg"
        elif kind == "image" and mime in CONVERT_TO_JPEG:
            sips = tools.need("sips", "part of macOS")
            target_jpg = Path(tmp) / (path.stem + ".jpg")
            proc = tools.run([sips, "-s", "format", "jpeg", str(path), "--out", str(target_jpg)], timeout=120)
            if proc.returncode != 0 or not target_jpg.exists():
                raise InvalidArgument("could not convert the image to JPEG (sips failed)")
            upload_path, upload_name, upload_mime = target_jpg, target_jpg.name, "image/jpeg"
        data = upload_path.read_bytes()
        if len(data) > limit:
            raise FileTooLarge(f"The converted file has {len(data)} bytes; GoWA accepts up to {limit}.",
                               limit_bytes=limit, size_bytes=len(data))

        route, field_name = ROUTES[kind]
        form: dict = {"phone": target["jid"]}
        if caption:
            form["caption"] = caption
        if reply_to:
            form["reply_message_id"] = str(reply_to).strip()
        if kind in ("audio", "voice"):
            form["ptt"] = "true" if kind == "voice" else "false"
        stamp = clock()
        usage = comp.reserve_send(env, stamp)
        try:
            res = client.results("POST", route, form=form, upload=(field_name, upload_name, upload_mime, data),
                                 timeout=300) or {}
        except comp.CompanionUnavailable as exc:
            raise comp.CompanionUnavailable(
                "GoWA did not answer the send. The file MAY have gone out: check the phone before retrying.",
                possibly_sent=True, **exc.details,
            ) from None
        except WAError:
            comp.release_send(env, stamp)
            raise
    message_id = res.get("message_id")
    if not message_id:
        raise comp.CompanionFailed("GoWA did not return the message id; check the phone before retrying.",
                                   gowa_status=res.get("status"))
    return {"sent": True, "mode": "sent", "via": "companion", "via_requested": via, "to": target,
            "file": {**file_info, "uploaded_bytes": len(data)}, "caption": caption,
            "reply_to": form.get("reply_message_id"),
            "confirmation": {"message_id": message_id, "by": "gowa"}, "rate": usage}
