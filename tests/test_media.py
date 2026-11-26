"""Media: metadata, wa media / wa_get_media, OCR, transcripts, cache and sending files.

Every file is generated here (PNG, WAV, PDF, DOCX, XLSX, PPTX, MP4) with made-up
content. WhatsApp and GoWA are simulated, and the real transcribers on this Mac are
never run: a fake mlx_whisper stands in for them.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from conftest import ago
from fake_gowa import SECRET, FakeGowa
from wa import cli, companion as comp, extract, mcp_server, media, media_send, transcribe
from wa.cache import Cache
from wa.core import WhatsApp
from wa.db import (
    FileTooLarge, InvalidArgument, MediaNotDownloaded, NotFoundError, PathRefused, SendRefused,
    TranscriberUnavailable, Unsupported,
)

LAB_JID = "5561944445555@s.whatsapp.net"
HAS_FFMPEG = bool(shutil.which("ffmpeg", path=extract._search_path(os.environ)))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
needs_vision = pytest.mark.skipif(not extract.ocr_status()[0], reason="pyobjc Vision not installed")
needs_mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only (sips)")

FONT_CANDIDATES = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
                   "/Library/Fonts/Arial.ttf")


# ------------------------------------------------------------ generated files


def _font(size):
    from PIL import ImageFont

    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def make_text_image(path: Path, text="INVOICE 4242", size=(1200, 400), noise=False):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, "white")
    if noise:
        img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    ImageDraw.Draw(img).text((60, size[1] // 3), text, fill="black", font=_font(120))
    img.save(path)
    return path


def make_wav(path: Path, seconds=1.0, rate=16000):
    import math
    import struct

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(rate * seconds))))
    return path


def make_pdf(path: Path, pages: list[list[str]]):
    """A small valid PDF with real text (Helvetica), one entry per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        content = "BT /F1 18 Tf 72 720 Td " + " ".join(f"({ln}) Tj 0 -24 Td" for ln in lines) + " ET"
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        content_no = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_no} 0 R "
                    "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out.getvalue())
    return path


def make_docx(path: Path):
    import docx

    d = docx.Document()
    d.add_paragraph("Meeting notes for the garden project")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Budget"
    t.rows[0].cells[1].text = "1500"
    d.save(path)
    return path


def make_xlsx(path: Path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Costs"
    wb.active.append(["Item", "Price"])
    wb.active.append(["Seeds", 12.5])
    wb.create_sheet("Extra").append(["Hidden sheet value"])
    wb.save(path)
    return path


def make_pptx(path: Path):
    from pptx import Presentation

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Quarterly plan"
    s.placeholders[1].text = "Plant tomatoes in March"
    prs.save(path)
    return path


def make_mp4(path: Path):
    ffmpeg = shutil.which("ffmpeg", path=extract._search_path(os.environ))
    cmd = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2", "-f", "lavfi",
           "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-shortest", "-y", str(path)]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        cmd[cmd.index("libx264")] = "mpeg4"
        subprocess.run(cmd, check=True, capture_output=True)
    return path


# ------------------------------------------------------------ synthetic WhatsApp with media


def _add(c, pk, chat, mtype, *, mime=None, size=None, dur=None, title=None, fname=None, rel=None, origin=0, text=None,
         hours=1.0):
    c.execute("INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZVCARDSTRING, ZFILESIZE, ZMOVIEDURATION, ZTITLE, ZAUTHORNAME, "
              "ZMEDIALOCALPATH, ZMEDIAORIGIN) VALUES (?,?,?,?,?,?,?,?,?)",
              (pk, pk, mime, size, dur, title, fname, rel, origin))
    c.execute("INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZISFROMME, ZMESSAGETYPE, ZFROMJID, "
              "ZSTANZAID, ZMEDIAITEM) VALUES (?,?,?,?,?,?,?,?,?)",
              (pk, chat, ago(hours=hours) + pk / 1000, text, 0, mtype, None, f"3A{pk:06d}", pk))


@pytest.fixture
def lab(data_dir):
    """Chat 'Media Lab' with one message per kind of media, plus files on 'disk'."""
    folder = data_dir / "Message" / "Media" / LAB_JID / "a" / "b"
    folder.mkdir(parents=True)
    files = {
        "photo.png": make_text_image(folder / "photo.png"),
        "voice.opus": make_wav(folder / "voice.opus"),
        "invoice.pdf": make_pdf(folder / "invoice.pdf", [["Invoice 4242", "Total due 99.90"] + [f"Line {i}: seeds, soil and water"
                                                                                      for i in range(8)],
                                                          ["Page two text here"],
                                                          ["Page three text"]]),
        "notes.docx": make_docx(folder / "notes.docx"),
        "costs.xlsx": make_xlsx(folder / "costs.xlsx"),
        "plan.pptx": make_pptx(folder / "plan.pptx"),
        "data.csv": folder / "data.csv",
        "archive.zip": folder / "archive.zip",
        "sticker.webp": folder / "sticker.webp",
    }
    files["data.csv"].write_text("name,score\nana,10\nbruno,7\n", encoding="utf-8")
    files["archive.zip"].write_bytes(b"PK\x03\x04 not really a zip")
    from PIL import Image

    Image.new("RGBA", (512, 512), (255, 0, 0, 128)).save(files["sticker.webp"])
    scan = folder / "scan.pdf"
    make_text_image(scan.with_suffix(".png"), "SCANNED 777")
    Image.open(scan.with_suffix(".png")).save(scan, "PDF", resolution=100)
    files["scan.pdf"] = scan
    if HAS_FFMPEG:
        files["clip.mp4"] = make_mp4(folder / "clip.mp4")
    rel = lambda name: f"Media/{LAB_JID}/a/b/{name}" if name in files else None  # noqa: E731

    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.execute("INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZLASTMESSAGEDATE) "
              "VALUES (10, ?, 'Media Lab', 0, ?)", (LAB_JID, ago(hours=0.1)))
    _add(c, 1001, 10, 1, mime="image/jpeg", size=34567, title="Receipt from the café", rel=rel("photo.png"))
    _add(c, 1002, 10, 1, mime="image/jpeg", size=1000)  # not downloaded
    _add(c, 1003, 10, 3, mime="audio/ogg; codecs=opus", size=3200, dur=1, origin=1, rel=rel("voice.opus"))
    _add(c, 1004, 10, 3, mime="audio/mpeg", size=99999, dur=180)
    _add(c, 1005, 10, 8, mime="application/pdf", size=900, dur=3, fname="invoice-4242.pdf", text="invoice-4242",
         title="Please pay this week", rel=rel("invoice.pdf"))
    _add(c, 1006, 10, 8, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         fname="notes.docx", text="notes", rel=rel("notes.docx"))
    _add(c, 1007, 10, 8, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         fname="costs.xlsx", text="costs", rel=rel("costs.xlsx"))
    _add(c, 1008, 10, 8, mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
         fname="plan.pptx", text="plan", rel=rel("plan.pptx"))
    _add(c, 1009, 10, 8, mime="text/csv", fname="data.csv", text="data", rel=rel("data.csv"))
    _add(c, 1010, 10, 8, mime="application/pdf", dur=1, fname="scan.pdf", text="scan", rel=rel("scan.pdf"))
    _add(c, 1011, 10, 2, mime="video/mp4", size=50000, dur=2, title="Look at this", rel=rel("clip.mp4"))
    _add(c, 1012, 10, 15, mime="image/webp", size=2000, rel=rel("sticker.webp"))
    c.execute("INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID) "
              "VALUES (1013, 10, ?, 'just text', 0, 'S1013')", (ago(hours=0.5),))
    _add(c, 1014, 10, 8, mime="application/zip", fname="archive.zip", text="archive", rel=rel("archive.zip"))
    _add(c, 1015, 10, 1, mime="image/jpeg", rel="../../../../etc/hosts")  # path escaping the media folder
    _add(c, 1100, 4, 1, mime="image/jpeg", rel=rel("photo.png"))  # hidden chat
    c.commit()
    c.close()
    return files


class FakeSTT:
    """Stands in for mlx_whisper; every other program is the real one."""

    def __init__(self, text="Hello from the voice note", fail=False):
        self.text = text
        self.fail = fail
        self.calls: list[list[str]] = []

    def which(self, name):
        if name == "mlx_whisper":
            return "/fake/mlx_whisper"
        if name in ("whisper-cli", "whisper-cpp"):
            return None
        return shutil.which(name, path=extract._search_path(os.environ))

    def __call__(self, cmd, **kw):
        if cmd[0] == "/fake/mlx_whisper":
            self.calls.append(cmd)
            if self.fail:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            out = Path(cmd[cmd.index("--output-dir") + 1]) / (cmd[cmd.index("--output-name") + 1] + ".json")
            out.write_text(json.dumps({"text": " " + self.text, "language": "en"}))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.run(cmd, **kw)


def tools_with(stt: FakeSTT, env=None) -> extract.Tools:
    return extract.Tools(env=env or os.environ, runner=stt, which=stt.which)


def no_transcriber() -> extract.Tools:
    real = lambda n: None if n in ("mlx_whisper", "whisper-cli", "whisper-cpp") else shutil.which(  # noqa: E731
        n, path=extract._search_path(os.environ))
    return extract.Tools(env=os.environ, which=real)


# ------------------------------------------------------------ metadata


def test_messages_carry_media_metadata(lab, wa):
    msgs = {m["id"]: m for m in wa.read_messages("Media Lab", limit=50)["messages"]}
    img = msgs[1001]["media"]
    assert img == {"kind": "image", "mime": "image/jpeg", "size_bytes": 34567, "caption": "Receipt from the café",
                   "downloaded": True}
    assert msgs[1002]["media"]["downloaded"] is False
    assert msgs[1003]["media"]["kind"] == "voice" and msgs[1003]["media"]["duration_s"] == 1
    assert msgs[1004]["media"]["kind"] == "audio" and msgs[1004]["media"]["duration_s"] == 180
    doc = msgs[1005]["media"]
    assert doc["kind"] == "document" and doc["file_name"] == "invoice-4242.pdf" and doc["pages"] == 3
    assert doc["title"] == "invoice-4242" and doc["caption"] == "Please pay this week"
    assert msgs[1011]["media"]["kind"] == "video" and msgs[1011]["media"]["caption"] == "Look at this"
    assert msgs[1012]["media"]["kind"] == "sticker"
    assert "media" not in msgs[1013]
    assert msgs[1015]["media"]["downloaded"] is False  # a path outside the media folder does not count


def test_media_filter_and_search_by_caption_and_file_name(lab, wa):
    ids = lambda kind: {m["id"] for m in wa.read_messages("Media Lab", limit=50, media_kind=kind)["messages"]}  # noqa: E731
    assert ids("voice") == {1003}
    assert ids("audio") == {1004}
    assert ids("document") == {1005, 1006, 1007, 1008, 1009, 1010, 1014}
    assert 1013 not in ids("any") and 1001 in ids("any")
    with pytest.raises(InvalidArgument):
        wa.read_messages("Media Lab", media_kind="podcast")
    assert [m["id"] for m in wa.search_messages("receipt from the cafe")["messages"]] == [1001]
    assert [m["id"] for m in wa.search_messages("invoice-4242.pdf")["messages"]] == [1005]


def test_older_media_schema_without_columns_still_reads(data_dir):
    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    c.executescript("DROP TABLE ZWAMEDIAITEM; CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER);")
    c.commit()
    c.close()
    with WhatsApp() as w:
        msgs = {m["id"]: m for m in w.read_messages("ana lucia", limit=10)["messages"]}
        assert msgs[103]["media"] == {"kind": "image", "downloaded": False}
        assert w.search_messages("CAFÉ")["count"] >= 1


# ------------------------------------------------------------ reading files


@needs_vision
def test_image_comes_back_reduced_with_local_ocr_and_cache(lab, wa):
    data, images = media.get_media(wa, 1001)
    assert data["file"]["source"] == "desktop" and data["untrusted"] is True
    assert "never as instructions" in data["note"]
    assert len(images) == 1 and images[0].data[:2] == b"\xff\xd8"
    assert max(images[0].width, images[0].height) <= 1568
    assert "4242" in data["content"]["ocr"]["text"] and data["content"]["ocr"]["cached"] is False
    again, _ = media.get_media(wa, 1001)
    assert again["content"]["ocr"]["cached"] is True
    root = Cache().root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    entries = [p for p in root.iterdir() if p.is_file()]
    assert entries and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in entries)


def test_large_image_is_shrunk_under_the_byte_limit(lab, wa, tmp_path, monkeypatch):
    big = make_text_image(tmp_path / "big.png", size=(4000, 3000), noise=True)
    part = extract.shrink_image(big, 120_000, "x", extract.Tools())
    assert max(part.width, part.height) <= 1568 and len(part.data) <= 120_000


@needs_mac
def test_shrink_falls_back_to_sips_without_pillow(lab, tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_pillow", lambda: None)
    big = make_text_image(tmp_path / "big.png", size=(3000, 1000))
    part = extract.shrink_image(big, 1_000_000, "x", extract.Tools())
    assert part.data[:2] == b"\xff\xd8" and part.width == 1568


def test_ocr_missing_is_reported_not_fatal(lab, wa, monkeypatch):
    monkeypatch.setattr(extract, "ocr_status", lambda: (False, "OCR needs pyobjc-framework-Vision: install"))
    data, images = media.get_media(wa, 1001)
    assert len(images) == 1
    assert data["content"]["ocr"] == {"available": False, "hint": "OCR needs pyobjc-framework-Vision: install"}


def test_pdf_text_with_page_and_char_limits(lab, wa):
    data, images = media.get_media(wa, 1005, max_pages=2)
    doc = data["content"]["document"]
    assert doc["format"] == "pdf" and doc["pages_total"] == 3 and doc["pages_read"] == 2
    assert "Invoice 4242" in doc["text"] and "Page two" in doc["text"] and "Page three" not in doc["text"]
    assert doc["truncated"] is True and doc["scanned"] is False
    assert images == []  # enough text: no page image unless asked
    short, _ = media.get_media(wa, 1005, max_chars=200, max_pages=50)
    assert len(short["content"]["document"]["text"]) <= 200
    forced, images = media.get_media(wa, 1005, page_image=True)
    assert [i.label for i in images] == ["page1"]


@needs_vision
def test_scanned_pdf_gets_ocr_and_first_page_image(lab, wa):
    data, images = media.get_media(wa, 1010)
    doc = data["content"]["document"]
    assert doc["scanned"] is True and doc["ocr"]["engine"] == "vision"
    assert "777" in doc["text"]
    assert [i.label for i in images] == ["page1"]


def test_office_and_text_documents(lab, wa):
    docx = media.get_media(wa, 1006)[0]["content"]["document"]
    assert "garden project" in docx["text"] and "Budget\t1500" in docx["text"]
    xlsx = media.get_media(wa, 1007, max_pages=1)[0]["content"]["document"]
    assert "## Sheet: Costs" in xlsx["text"] and "Seeds\t12.5" in xlsx["text"]
    assert xlsx["sheets_total"] == 2 and "Hidden sheet value" not in xlsx["text"] and xlsx["truncated"]
    pptx = media.get_media(wa, 1008)[0]["content"]["document"]
    assert "Quarterly plan" in pptx["text"] and "Plant tomatoes" in pptx["text"]
    csv = media.get_media(wa, 1009)[0]["content"]["document"]
    assert csv["reader"] == "plain" and "bruno,7" in csv["text"]
    unknown = media.get_media(wa, 1014)[0]["content"]["document"]
    assert unknown["format"] is None and "no text extractor" in unknown["note"]


def test_sticker_is_an_image(lab, wa):
    data, images = media.get_media(wa, 1012, ocr=False)
    assert len(images) == 1 and "ocr" not in data["content"]


def test_voice_note_transcript_is_local_and_cached(lab, wa):
    stt = FakeSTT()
    data, images = media.get_media(wa, 1003, tools=tools_with(stt))
    t = data["content"]["transcript"]
    assert t["text"] == "Hello from the voice note" and t["engine"] == "mlx" and t["local"] is True
    assert t["model"] == transcribe.MLX_DEFAULT_MODEL and t["cached"] is False and images == []
    assert "--language" not in stt.calls[0]
    again, _ = media.get_media(wa, 1003, tools=tools_with(stt))
    assert again["content"]["transcript"]["cached"] is True and len(stt.calls) == 1


def test_transcript_model_and_language_settings(lab, wa, monkeypatch):
    monkeypatch.setenv("WA_TRANSCRIBE_MODEL", "mlx-community/whisper-small")
    monkeypatch.setenv("WA_TRANSCRIBE_LANGUAGE", "pt")
    stt = FakeSTT()
    t = media.get_media(wa, 1003, tools=tools_with(stt))[0]["content"]["transcript"]
    cmd = stt.calls[0]
    assert cmd[cmd.index("--model") + 1] == "mlx-community/whisper-small"
    assert cmd[cmd.index("--language") + 1] == "pt" and t["model"] == "mlx-community/whisper-small"


def test_no_transcriber_is_an_error_with_install_hint(lab, wa):
    with pytest.raises(TranscriberUnavailable) as exc:
        media.get_media(wa, 1003, tools=no_transcriber())
    assert exc.value.code == "transcriber_unavailable" and "mlx-whisper" in exc.value.message
    assert "WA_TRANSCRIBE_REMOTE=1" in exc.value.details["install"]


def test_openai_only_with_explicit_opt_in(lab, wa, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(TranscriberUnavailable):  # a key alone is not consent
        media.get_media(wa, 1003, tools=no_transcriber())

    monkeypatch.setenv("WA_TRANSCRIBE_REMOTE", "1")
    sent = {}

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        sent["url"] = req.full_url
        sent["auth"] = req.headers["Authorization"]
        sent["body"] = req.data
        return Resp(json.dumps({"text": "remote words", "language": "english"}).encode())

    data, _ = media.get_media(wa, 1003, tools=no_transcriber(), opener=opener)
    t = data["content"]["transcript"]
    assert t["engine"] == "openai" and t["local"] is False and t["text"] == "remote words"
    assert sent["url"] == transcribe.OPENAI_URL and sent["auth"] == "Bearer sk-test"
    assert b'name="model"\r\n\r\nwhisper-1' in sent["body"] and b'filename="voice.ogg"' in sent["body"]


def test_whisper_cpp_engine(lab, wa, tmp_path, monkeypatch):
    model = tmp_path / "ggml-base.bin"
    model.write_bytes(b"model")
    monkeypatch.setenv("WA_WHISPER_CPP_MODEL", str(model))
    seen = []

    def which(name):
        if name == "whisper-cli":
            return "/fake/whisper-cli"
        if name in ("mlx_whisper", "whisper-cpp"):
            return None
        return shutil.which(name, path=extract._search_path(os.environ))

    def runner(cmd, **kw):
        if cmd[0] == "/fake/whisper-cli":
            seen.append(cmd)
            Path(cmd[cmd.index("-of") + 1] + ".txt").write_text("bom dia\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0].endswith("ffmpeg"):  # the wav conversion
            Path(cmd[-1]).write_bytes(b"RIFF")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, **kw)

    tools = extract.Tools(env=os.environ, runner=runner, which=which)
    if not tools.find("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    t = media.get_media(wa, 1003, tools=tools)[0]["content"]["transcript"]
    assert t["engine"] == "whisper.cpp" and t["text"] == "bom dia" and t["model"] == "ggml-base.bin"
    assert seen[0][seen[0].index("-m") + 1] == str(model) and seen[0][seen[0].index("-l") + 1] == "auto"


@needs_ffmpeg
def test_video_gives_frames_and_transcript(lab, wa):
    stt = FakeSTT("sound of the video")
    data, images = media.get_media(wa, 1011, frames=3, tools=tools_with(stt))
    assert len(images) == 3 and all(i.label.startswith("frame@") for i in images)
    assert data["content"]["video"]["has_audio"] is True
    assert data["content"]["transcript"]["text"] == "sound of the video"
    none, _ = media.get_media(wa, 1011, frames=0, transcribe=False, tools=tools_with(stt))
    assert "transcript" not in none["content"]


@needs_ffmpeg
def test_video_without_transcriber_still_returns_frames(lab, wa):
    data, images = media.get_media(wa, 1011, frames=2, tools=no_transcriber())
    assert len(images) == 2 and data["content"]["transcript"]["error"] == "transcriber_unavailable"


def test_missing_message_no_media_hidden_chat_and_bad_path(lab, wa):
    with pytest.raises(NotFoundError):
        media.get_media(wa, 999999)
    with pytest.raises(InvalidArgument):
        media.get_media(wa, 1013)
    with pytest.raises(InvalidArgument):
        media.get_media(wa, "abc")
    with pytest.raises(NotFoundError):  # hidden chats stay hidden (the MCP never includes them)
        media.get_media(wa, 1100)
    with pytest.raises(MediaNotDownloaded):
        media.get_media(wa, 1015)


def test_not_downloaded_without_companion(lab, wa):
    with pytest.raises(MediaNotDownloaded) as exc:
        media.get_media(wa, 1002, client_factory=lambda: comp.GowaClient("http://127.0.0.1:9", None))
    assert exc.value.code == "media_not_downloaded" and "Open the chat in WhatsApp" in exc.value.message


@pytest.fixture
def gowa(tmp_path, monkeypatch):
    with FakeGowa() as g:
        g.media_root = tmp_path / "gowa-data"
        monkeypatch.setenv("WA_GOWA_DIR", str(g.media_root))
        yield g


def test_companion_downloads_missing_media(lab, wa, gowa, tmp_path):
    png = make_text_image(tmp_path / "remote.png", "FROM COMPANION")
    gowa.downloads["3A001002"] = (LAB_JID, "remote.png", png.read_bytes())
    client = comp.GowaClient(gowa.url, SECRET)
    data, images = media.get_media(wa, 1002, ocr=False, client_factory=lambda: client)
    assert data["file"]["source"] == "companion" and len(images) == 1
    assert data["file"]["path"].startswith(str(gowa.media_root.resolve()))
    call = [c for c in gowa.calls if c["path"].endswith("/download")][0]
    assert call["path"] == "/message/3A001002/download" and call["query"] == {"phone": LAB_JID}
    with pytest.raises(MediaNotDownloaded):
        media.get_media(wa, 1002, download=False, client_factory=lambda: client)


def test_companion_download_outside_its_folder_is_refused(lab, wa, gowa, tmp_path):
    gowa.downloads["3A001002"] = (LAB_JID, "x.png", b"data")
    gowa.download_path_override = "../../outside.png"
    client = comp.GowaClient(gowa.url, SECRET)
    with pytest.raises(MediaNotDownloaded) as exc:
        media.get_media(wa, 1002, client_factory=lambda: client)
    assert "outside" in exc.value.details["companion_error"]


# ------------------------------------------------------------ CLI and MCP


def _cli(capsys, *args):
    rc = cli.run(list(args))
    return rc, json.loads(capsys.readouterr().out)


def test_cli_media_saves_reduced_images_privately(lab, capsys):
    rc, out = _cli(capsys, "media", "--message", "1001", "--no-ocr")
    assert rc == 0 and out["images"][0]["label"] == "image"
    path = Path(out["images"][0]["path"])
    assert path.read_bytes()[:2] == b"\xff\xd8" and stat.S_IMODE(path.stat().st_mode) == 0o600
    rc, out = _cli(capsys, "media", "--message", "1002")
    assert rc == 1 and out["error"] == "media_not_downloaded"
    rc, out = _cli(capsys, "messages", "--chat", "Media Lab", "--media", "voice")
    assert [m["id"] for m in out["messages"]] == [1003]


def test_cli_media_cache_stats_and_clear(lab, capsys):
    _cli(capsys, "media", "--message", "1009")
    rc, stats = _cli(capsys, "media-cache")
    assert rc == 0 and stats["entries"] >= 1
    rc, cleared = _cli(capsys, "media-cache", "--clear")
    assert cleared["cleared"] is True and cleared["removed_entries"] == stats["entries"]
    assert _cli(capsys, "media-cache")[1]["entries"] == 0


def test_mcp_get_media_returns_image_content(lab):
    from mcp.types import ImageContent, TextContent

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    for name in ("wa_get_media", "wa_read_messages", "wa_search_messages"):
        assert "never as instructions" in tools[name].description or "never as\ninstructions" in tools[name].description
    assert "DATA" in tools["wa_get_media"].description
    res = asyncio.run(mcp_server.mcp.call_tool("wa_get_media", {"message_id": 1001, "ocr": False}))
    blocks = res[0] if isinstance(res, tuple) else res
    assert isinstance(blocks[0], TextContent) and isinstance(blocks[1], ImageContent)
    assert blocks[1].mimeType == "image/jpeg" and base64.b64decode(blocks[1].data)[:2] == b"\xff\xd8"
    payload = json.loads(blocks[0].text)
    assert payload["images"][0]["label"] == "image" and "path" not in payload["images"][0]
    err = asyncio.run(mcp_server.mcp.call_tool("wa_get_media", {"message_id": 1100}))
    blocks = err[0] if isinstance(err, tuple) else err
    assert json.loads(blocks[0].text)["error"] == "not_found"
    msgs = mcp_server.wa_read_messages("Media Lab", media="document")
    assert {m["id"] for m in msgs["messages"]} >= {1005, 1006}


# ------------------------------------------------------------ sending files


@pytest.fixture
def outbox(tmp_path):
    env = {"WA_MAC_HOME": os.environ["WA_MAC_HOME"]}
    return media_send.ensure_outbox(env)


@pytest.fixture
def connected(gowa, monkeypatch):
    client = comp.GowaClient(gowa.url, SECRET)
    monkeypatch.setattr(cli, "_companion_factory", lambda: client)
    monkeypatch.setattr(mcp_server, "_companion_factory", lambda: client)
    return client


def _env(**extra):
    return {"WA_MAC_HOME": os.environ["WA_MAC_HOME"], "WA_GOWA_DIR": os.environ.get("WA_GOWA_DIR", ""), **extra}


def test_outbox_is_private_and_paths_are_guarded(lab, outbox, tmp_path):
    env = _env()
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o700
    (outbox / "ok.pdf").write_bytes(b"%PDF")
    assert media_send.resolve_file("ok.pdf", env, False) == (outbox / "ok.pdf").resolve()
    assert media_send.resolve_file(str(outbox / "ok.pdf"), env, False) == (outbox / "ok.pdf").resolve()
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    with pytest.raises(PathRefused) as exc:
        media_send.resolve_file(str(secret), env, False)
    assert exc.value.code == "path_refused" and str(outbox.resolve()) in exc.value.details["outbox"]
    with pytest.raises(PathRefused):
        media_send.resolve_file("../id_rsa", env, False)
    with pytest.raises(PathRefused):
        media_send.resolve_file(f"{outbox}/../id_rsa", env, True)  # '..' is refused even when any path is allowed
    (outbox / "link.txt").symlink_to(secret)
    with pytest.raises(PathRefused):
        media_send.resolve_file("link.txt", env, False)
    (outbox / "inner.txt").symlink_to(outbox / "ok.pdf")
    assert media_send.resolve_file("inner.txt", env, False) == (outbox / "ok.pdf").resolve()
    assert media_send.resolve_file(str(secret), env, True) == secret.resolve()
    (outbox / "dir").mkdir()
    with pytest.raises(InvalidArgument):
        media_send.resolve_file("dir", env, False)
    with pytest.raises(NotFoundError):
        media_send.resolve_file("nope.pdf", env, False)


def test_draft_shows_file_kind_size_and_recipient(lab, wa, outbox, gowa, connected):
    make_pdf(outbox / "report.pdf", [["hello"]])
    out = media_send.send_media(wa, "+55 61 94444-5555", "report.pdf", caption="Here it is", env=_env(),
                                client_factory=lambda: connected)
    assert out["sent"] is False and out["mode"] == "draft" and out["via"] == "companion"
    assert out["file"]["name"] == "report.pdf" and out["file"]["kind"] == "document"
    assert out["file"]["size_bytes"] == (outbox / "report.pdf").stat().st_size and out["file"]["kind_inferred"]
    assert out["to"]["jid"] == LAB_JID and out["caption"] == "Here it is"
    assert out["limit_bytes"] == 50_000_000 and out["limit_source"] == "gowa" and out["within_limit"]
    assert out["send_allowed_by_env"] is False
    assert not gowa.posts("/send/")  # a draft sends nothing


def test_guards_desktop_env_and_companion(lab, wa, outbox, gowa, connected):
    (outbox / "a.png").write_bytes(make_text_image(outbox / "a.png").read_bytes())
    with pytest.raises(Unsupported) as exc:
        media_send.send_media(wa, LAB_JID, "a.png", via="desktop", env=_env(WA_ALLOW_SEND="1"))
    assert exc.value.code == "unsupported"
    with pytest.raises(SendRefused):
        media_send.send_media(wa, LAB_JID, "a.png", confirm=True, env=_env(), client_factory=lambda: connected)
    off = lambda: comp.GowaClient("http://127.0.0.1:9", SECRET)  # noqa: E731
    with pytest.raises(comp.CompanionUnavailable):
        media_send.send_media(wa, LAB_JID, "a.png", confirm=True, env=_env(WA_ALLOW_SEND="1"), client_factory=off)
    draft = media_send.send_media(wa, LAB_JID, "a.png", env=_env(), client_factory=off)
    assert "not connected" in draft["via_note"]
    assert not gowa.posts("/send/")


def test_send_image_and_document_through_gowa(lab, wa, outbox, gowa, connected):
    make_text_image(outbox / "pic.png")
    out = media_send.send_media(wa, LAB_JID, "pic.png", caption="Look", confirm=True, env=_env(WA_ALLOW_SEND="1"),
                                client_factory=lambda: connected)
    assert out["sent"] is True and out["confirmation"]["message_id"].startswith("3EB0MEDIA")
    call = gowa.posts("/send/image")[0]
    assert call["form"] == {"phone": LAB_JID, "caption": "Look"}
    assert call["upload"]["field"] == "image" and call["upload"]["filename"] == "pic.png"
    assert call["upload"]["data"] == (outbox / "pic.png").read_bytes()
    assert call["upload"]["content_type"] == "image/png"

    make_pdf(outbox / "doc.pdf", [["x"]])
    media_send.send_media(wa, LAB_JID, "doc.pdf", confirm=True, reply_to="3A000001", env=_env(WA_ALLOW_SEND="1"),
                          client_factory=lambda: connected)
    call = gowa.posts("/send/file")[0]
    assert call["upload"]["field"] == "file" and call["form"]["reply_message_id"] == "3A000001"


@needs_ffmpeg
def test_voice_note_is_converted_to_ogg_opus(lab, wa, outbox, gowa, connected):
    make_wav(outbox / "memo.wav")
    out = media_send.send_media(wa, LAB_JID, "memo.wav", as_kind="voice", confirm=True,
                                env=_env(WA_ALLOW_SEND="1"), client_factory=lambda: connected)
    assert out["sent"] and out["file"]["conversion"].startswith("ffmpeg")
    call = gowa.posts("/send/audio")[0]
    assert call["form"]["ptt"] == "true" and call["upload"]["filename"] == "voice.ogg"
    assert call["upload"]["data"][:4] == b"OggS" and call["upload"]["content_type"] == "audio/ogg"


def test_kind_and_caption_rules(lab, wa, outbox, connected):
    make_wav(outbox / "memo.wav")
    with pytest.raises(InvalidArgument):
        media_send.send_media(wa, LAB_JID, "memo.wav", caption="no", env=_env(), client_factory=lambda: connected)
    with pytest.raises(InvalidArgument):
        media_send.send_media(wa, LAB_JID, "memo.wav", as_kind="image", env=_env(), client_factory=lambda: connected)
    with pytest.raises(InvalidArgument):
        media_send.send_media(wa, LAB_JID, "memo.wav", as_kind="gif", env=_env(), client_factory=lambda: connected)
    out = media_send.send_media(wa, LAB_JID, "memo.wav", env=_env(), client_factory=lambda: connected)
    assert out["file"]["kind"] == "audio"


def test_size_limit_from_app_info(lab, wa, outbox, gowa, connected):
    gowa.app_info["max_image_size"] = 100
    make_text_image(outbox / "pic.png")
    draft = media_send.send_media(wa, LAB_JID, "pic.png", env=_env(), client_factory=lambda: connected)
    assert draft["within_limit"] is False and draft["limit_bytes"] == 100 and "larger" in draft["note"]
    with pytest.raises(FileTooLarge) as exc:
        media_send.send_media(wa, LAB_JID, "pic.png", confirm=True, env=_env(WA_ALLOW_SEND="1"),
                              client_factory=lambda: connected)
    assert exc.value.code == "file_too_large" and not gowa.posts("/send/")


def test_media_sends_count_against_the_rate_limit(lab, wa, outbox, gowa, connected):
    (outbox / "n.txt").write_text("note")
    env = _env(WA_ALLOW_SEND="1", WA_COMPANION_MAX_PER_MINUTE="1")
    media_send.send_media(wa, LAB_JID, "n.txt", confirm=True, env=env, client_factory=lambda: connected)
    with pytest.raises(comp.RateLimited):
        media_send.send_media(wa, LAB_JID, "n.txt", confirm=True, env=env, client_factory=lambda: connected)
    assert len(gowa.posts("/send/")) == 1


def test_cli_send_file(lab, outbox, gowa, connected, capsys, tmp_path, monkeypatch):
    (outbox / "n.txt").write_text("note")
    rc, out = _cli(capsys, "send", "--to", LAB_JID, "--file", "n.txt")
    assert rc == 0 and out["mode"] == "draft" and out["file"]["kind"] == "document"
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    rc, out = _cli(capsys, "send", "--to", LAB_JID, "--file", str(outside))
    assert rc == 1 and out["error"] == "path_refused"
    rc, out = _cli(capsys, "send", "--to", LAB_JID, "--file", str(outside), "--allow-any-path")
    assert rc == 0 and out["file"]["path"] == str(outside.resolve())
    rc, out = _cli(capsys, "send", "--to", LAB_JID, "--file", "n.txt", "--via", "desktop")
    assert rc == 1 and out["error"] == "unsupported"
    rc, out = _cli(capsys, "send", "--to", LAB_JID, "--file", "n.txt", "--text", "hi")
    assert rc == 1 and out["error"] == "invalid_argument"
    rc, out = _cli(capsys, "send", "--to", LAB_JID)
    assert rc == 1 and out["error"] == "invalid_argument"
    monkeypatch.setenv("WA_ALLOW_SEND", "1")
    rc, out = _cli(capsys, "send", "--to", LAB_JID, "--file", "n.txt", "--confirm")
    assert rc == 0 and out["sent"] is True


def test_mcp_send_media_only_from_outbox_unless_allowed(lab, outbox, gowa, connected, tmp_path, monkeypatch):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    assert mcp_server.wa_send_media(LAB_JID, str(outside))["error"] == "path_refused"
    monkeypatch.setenv("WA_ALLOW_ANY_FILE", "1")
    assert mcp_server.wa_send_media(LAB_JID, str(outside))["mode"] == "draft"
    monkeypatch.delenv("WA_ALLOW_ANY_FILE")
    (outbox / "n.txt").write_text("note")
    assert mcp_server.wa_send_media(LAB_JID, "n.txt", confirm=True)["error"] == "send_refused"
    assert mcp_server.wa_send_media(LAB_JID, "n.txt", caption="x" * 2001)["error"] == "invalid_argument"
    assert not gowa.posts("/send/")
