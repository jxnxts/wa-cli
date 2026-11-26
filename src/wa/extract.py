"""Turns a media file into something an agent can read: reduced images, document text,
OCR and video frames. Everything runs on this Mac.

Optional pieces, each detected when it is needed:
- Pillow reduces images; without it, macOS `sips` does the same job.
- pypdf, python-docx, openpyxl and python-pptx read documents. Without them, PDF text
  comes from PDFKit (pyobjc) and Word, RTF, ODT and HTML from macOS `textutil`.
- pyobjc-framework-Vision runs OCR with the Vision framework and renders PDF pages
  with Quartz.
- ffmpeg/ffprobe pull frames and audio out of videos and convert voice notes.

The `wa-mac[media]` extra installs the Python ones. ffmpeg comes from Homebrew.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .db import InvalidArgument, MissingDependency

MAX_SIDE = 1568  # longest side of an image handed to the model
DEFAULT_IMAGE_MAX_BYTES = 1_000_000
MIN_IMAGE_MAX_BYTES = 50_000
DEFAULT_MAX_PAGES = 10
MAX_PAGES = 50
DEFAULT_MAX_CHARS = 20_000
MAX_CHARS = 200_000
DEFAULT_FRAMES = 4
MAX_FRAMES = 8
EMPTY_PAGE_CHARS = 10  # a PDF page with less text than this is an image: it gets OCR
SHORT_TEXT_CHARS = 200

INSTALL_MEDIA = "uv tool install --force --reinstall '.[media]'  (from the wa-mac clone)"
INSTALL_FFMPEG = "brew install ffmpeg"

EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"), "/usr/bin")

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".ini", ".toml",
    ".py", ".js", ".ts", ".sql", ".sh", ".html", ".htm", ".ics", ".vcf", ".srt", ".vtt",
}
TEXTUTIL_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt", ".html", ".htm", ".webarchive", ".wordml"}


# ------------------------------------------------------------ tools


def _search_path(env: Mapping[str, str]) -> str:
    return os.pathsep.join([env.get("PATH", ""), *EXTRA_PATH])


@dataclass
class Tools:
    """External programs, swappable in tests."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    which: Callable[[str], str | None] | None = None

    def find(self, name: str) -> str | None:
        if self.which is not None:
            return self.which(name)
        return shutil.which(name, path=_search_path(self.env))

    def need(self, name: str, hint: str) -> str:
        found = self.find(name)
        if not found:
            raise MissingDependency(f"{name} not found. Install it: {hint}", missing=name, install=hint)
        return found

    def run(self, cmd: list[str], timeout: float = 120, **kw) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env["PATH"] = _search_path(self.env)
        return self.runner(cmd, capture_output=True, text=kw.pop("text", True), timeout=timeout, env=env, **kw)


def clamp(value, default: int, low: int, high: int, name: str) -> int:
    if value is None:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise InvalidArgument(f"{name} must be an integer") from None
    return max(low, min(value, high))


def image_max_bytes(env: Mapping[str, str]) -> int:
    raw = env.get("WA_MEDIA_IMAGE_MAX_BYTES")
    if not raw:
        return DEFAULT_IMAGE_MAX_BYTES
    try:
        return max(MIN_IMAGE_MAX_BYTES, int(raw))
    except ValueError:
        raise InvalidArgument("WA_MEDIA_IMAGE_MAX_BYTES must be an integer") from None


# ------------------------------------------------------------ images


@dataclass
class ImagePart:
    data: bytes
    width: int
    height: int
    label: str
    path: str | None = None

    def meta(self) -> dict:
        out = {"label": self.label, "width": self.width, "height": self.height, "bytes": len(self.data)}
        if self.path:
            out["path"] = self.path
        return out


def _pillow():
    try:
        from PIL import Image, ImageOps  # noqa: F401
    except ImportError:
        return None
    return Image, ImageOps


def _shrink_pillow(src: Path, max_bytes: int, label: str) -> ImagePart:
    Image, ImageOps = _pillow()
    with Image.open(src) as im:
        try:
            im.seek(0)  # first frame of an animated sticker or GIF
        except EOFError:
            pass
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            im = Image.new("RGB", rgba.size, "white")
            im.paste(rgba, mask=rgba.split()[-1])
        else:
            im = im.convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    data = b""
    while True:
        for quality in (85, 75, 65, 55, 45):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return ImagePart(data, im.width, im.height, label)
        if min(im.size) <= 64:
            return ImagePart(data, im.width, im.height, label)
        im = im.resize((max(1, int(im.width * 0.75)), max(1, int(im.height * 0.75))), Image.LANCZOS)


def _sips_size(tools: Tools, path: Path) -> tuple[int, int]:
    proc = tools.run([tools.need("sips", "part of macOS"), "-g", "pixelWidth", "-g", "pixelHeight", str(path)], timeout=60)
    w = re.search(r"pixelWidth:\s*(\d+)", proc.stdout or "")
    h = re.search(r"pixelHeight:\s*(\d+)", proc.stdout or "")
    if proc.returncode != 0 or not (w and h):
        raise MissingDependency("could not read the image (sips failed and Pillow is not installed)",
                                install=INSTALL_MEDIA, detail=(proc.stderr or "")[:200])
    return int(w.group(1)), int(h.group(1))


def _shrink_sips(src: Path, max_bytes: int, label: str, tools: Tools) -> ImagePart:
    sips = tools.need("sips", "part of macOS")
    w, h = _sips_size(tools, src)
    side = min(MAX_SIDE, max(w, h))
    with tempfile.TemporaryDirectory(prefix="wa-img-") as tmp:
        out = Path(tmp) / "out.jpg"
        data = b""
        while True:
            for quality in (80, 65, 50, 40):
                proc = tools.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
                                  "-Z", str(side), str(src), "--out", str(out)], timeout=120)
                if proc.returncode != 0 or not out.exists():
                    raise MissingDependency("could not convert the image (sips failed and Pillow is not installed)",
                                            install=INSTALL_MEDIA, detail=(proc.stderr or "")[:200])
                data = out.read_bytes()
                if len(data) <= max_bytes:
                    ow, oh = _sips_size(tools, out)
                    return ImagePart(data, ow, oh, label)
            if side <= 64:
                ow, oh = _sips_size(tools, out)
                return ImagePart(data, ow, oh, label)
            side = int(side * 0.75)


def shrink_image(src: Path, max_bytes: int, label: str, tools: Tools) -> ImagePart:
    """JPEG with the longest side ≤ 1568 px and at most max_bytes."""
    if _pillow() is not None:
        try:
            return _shrink_pillow(src, max_bytes, label)
        except Exception:  # noqa: BLE001 - HEIC and other formats Pillow cannot open: try sips
            pass
    return _shrink_sips(src, max_bytes, label, tools)


# ------------------------------------------------------------ OCR (Vision)


def ocr_status() -> tuple[bool, str | None]:
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        return False, f"OCR needs pyobjc-framework-Vision: {INSTALL_MEDIA}"
    return True, None


def ocr_languages(env: Mapping[str, str]) -> list[str]:
    raw = env.get("WA_OCR_LANGUAGES") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def _ocr_cgimage(cgimage, languages: list[str]) -> str:
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    if languages:
        request.setRecognitionLanguages_(languages)
    elif hasattr(request, "setAutomaticallyDetectsLanguage_"):
        request.setAutomaticallyDetectsLanguage_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(str(err))
    lines = []
    for obs in request.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        box = obs.boundingBox()
        lines.append((-round(box.origin.y, 2), box.origin.x, str(cands[0].string())))
    lines.sort()
    return "\n".join(t for _, _, t in lines)


def _load_cgimage(path: Path):
    import Quartz
    from Foundation import NSURL

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    if src is None:
        raise RuntimeError("could not open the image")
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        raise RuntimeError("could not decode the image")
    return img


def ocr_image(path: Path, env: Mapping[str, str]) -> str:
    import objc

    with objc.autorelease_pool():
        return _ocr_cgimage(_load_cgimage(path), ocr_languages(env))


def _pdf_document(path: Path):
    import Quartz
    from Foundation import NSURL

    doc = Quartz.CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(str(path)))
    if doc is None:
        raise RuntimeError("could not open the PDF")
    return doc


def _render_pdf_page(doc, index: int, max_side: int = 2000):
    import Quartz

    page = Quartz.CGPDFDocumentGetPage(doc, index + 1)
    if page is None:
        raise RuntimeError(f"page {index + 1} not found")
    box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFCropBox)
    w, h = box.size.width or 612, box.size.height or 792
    scale = max_side / max(w, h)
    pw, ph = max(1, int(w * scale)), max(1, int(h * scale))
    ctx = Quartz.CGBitmapContextCreate(None, pw, ph, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
                                       Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, pw, ph))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextTranslateCTM(ctx, -box.origin.x, -box.origin.y)
    Quartz.CGContextDrawPDFPage(ctx, page)
    return Quartz.CGBitmapContextCreateImage(ctx)


def _write_jpeg(cgimage, out: Path) -> None:
    import Quartz
    from Foundation import NSURL

    dest = Quartz.CGImageDestinationCreateWithURL(NSURL.fileURLWithPath_(str(out)), "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(dest, cgimage, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError("could not write the page image")


def ocr_pdf(path: Path, indexes: list[int], env: Mapping[str, str]) -> dict[int, str]:
    """OCR of some pages (0-based), rendered with Quartz."""
    import objc

    out = {}
    langs = ocr_languages(env)
    with objc.autorelease_pool():
        doc = _pdf_document(path)
        for i in indexes:
            with objc.autorelease_pool():
                out[i] = _ocr_cgimage(_render_pdf_page(doc, i), langs)
    return out


def pdf_first_page_image(path: Path, out: Path, tools: Tools) -> Path:
    """First page as JPEG: Quartz when pyobjc is installed, otherwise `sips` (which renders page 1)."""
    if ocr_status()[0]:
        try:
            import objc

            with objc.autorelease_pool():
                _write_jpeg(_render_pdf_page(_pdf_document(path), 0), out)
            return out
        except Exception:  # noqa: BLE001 - fall back to sips
            pass
    sips = tools.need("sips", "part of macOS")
    proc = tools.run([sips, "-s", "format", "jpeg", str(path), "--out", str(out)], timeout=120)
    if proc.returncode != 0 or not out.exists():
        raise MissingDependency("could not render the PDF page", detail=(proc.stderr or "")[:200])
    return out


# ------------------------------------------------------------ documents


def doc_format(mime: str | None, name: str | None) -> str | None:
    mime = (mime or "").split(";")[0].strip().lower()
    ext = Path(name or "").suffix.lower()
    if mime == "application/pdf" or ext == ".pdf":
        return "pdf"
    if "wordprocessingml" in mime or ext == ".docx":
        return "docx"
    if "spreadsheetml" in mime or ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if "presentationml" in mime or ext == ".pptx":
        return "pptx"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if ext in TEXTUTIL_EXTENSIONS - {".html", ".htm"} or mime in (
        "application/msword", "application/rtf", "text/rtf", "application/vnd.oasis.opendocument.text",
    ):
        return "textutil"
    if (mime.startswith("text/") or mime in ("application/json", "application/xml", "application/x-sql",
                                             "application/sql", "application/javascript")
            or ext in TEXT_EXTENSIONS):
        return "text"
    return None


def _cap(parts: list[str], max_chars: int) -> tuple[str, bool]:
    text = "\n".join(p for p in parts if p is not None).strip()
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _pdf_text(path: Path, max_pages: int) -> tuple[list[str], int, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None  # noqa: N806
    if PdfReader is not None:
        reader = PdfReader(str(path))
        total = len(reader.pages)
        pages = []
        for i in range(min(max_pages, total)):
            try:
                pages.append(reader.pages[i].extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page should not lose the rest
                pages.append("")
        return pages, total, "pypdf"
    try:
        from Foundation import NSURL
        from Quartz import PDFDocument
    except ImportError:
        raise MissingDependency(f"reading PDFs needs pypdf (or pyobjc): {INSTALL_MEDIA}", install=INSTALL_MEDIA) from None
    doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(path)))
    if doc is None:
        raise InvalidArgument("could not open the PDF")
    total = int(doc.pageCount())
    return [str(doc.pageAtIndex_(i).string() or "") for i in range(min(max_pages, total))], total, "pdfkit"


def _docx_text(path: Path, tools: Tools) -> tuple[list[str], str]:
    try:
        import docx
    except ImportError:
        return [_textutil(path, tools)], "textutil"
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append("\t".join(c.text.strip() for c in row.cells))
    return parts, "python-docx"


def _xlsx_text(path: Path, max_pages: int, max_chars: int) -> tuple[list[str], int, int]:
    try:
        import openpyxl
    except ImportError:
        raise MissingDependency(f"reading XLSX needs openpyxl: {INSTALL_MEDIA}", install=INSTALL_MEDIA) from None
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts, used, read = [], 0, 0
        for ws in wb.worksheets[:max_pages]:
            read += 1
            parts.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                if all(v is None for v in row):
                    continue
                line = "\t".join("" if v is None else str(v) for v in row).rstrip()
                parts.append(line)
                used += len(line) + 1
                if used > max_chars:
                    break
            if used > max_chars:
                break
        return parts, len(wb.worksheets), read
    finally:
        wb.close()


def _pptx_text(path: Path, max_pages: int) -> tuple[list[str], int, int]:
    try:
        from pptx import Presentation
    except ImportError:
        raise MissingDependency(f"reading PPTX needs python-pptx: {INSTALL_MEDIA}", install=INSTALL_MEDIA) from None
    prs = Presentation(str(path))
    slides = list(prs.slides)
    parts = []
    for n, slide in enumerate(slides[:max_pages], 1):
        parts.append(f"## Slide {n}")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    parts.append("\t".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[notes] {notes}")
    return parts, len(slides), min(max_pages, len(slides))


def _textutil(path: Path, tools: Tools) -> str:
    tu = tools.need("textutil", "part of macOS")
    proc = tools.run([tu, "-convert", "txt", "-stdout", str(path)], timeout=120)
    if proc.returncode != 0:
        raise InvalidArgument("could not read the document (textutil failed)", detail=(proc.stderr or "")[:200])
    return proc.stdout or ""


def _plain_text(path: Path, max_chars: int) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(max_chars * 4 + 4)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    text = raw.decode("utf-8-sig", errors="replace")
    if text.count("�") > len(text) // 100 + 3:  # not UTF-8 after all
        return raw.decode("latin-1")
    return text


def extract_document(path: Path, fmt: str, max_pages: int, max_chars: int, tools: Tools,
                     ocr: bool, env: Mapping[str, str]) -> dict:
    """Text of a document. For a PDF with almost no text, OCR of the first pages."""
    out: dict = {"format": fmt}
    if fmt == "pdf":
        pages, total, reader = _pdf_text(path, max_pages)
        out.update(pages_total=total, pages_read=len(pages), reader=reader)
        empty = [i for i, p in enumerate(pages) if len(p.strip()) < EMPTY_PAGE_CHARS]
        out["scanned"] = bool(pages) and len(empty) == len(pages)
        if empty:
            out["pages_without_text"] = [i + 1 for i in empty]
        if empty and ocr:
            ok, hint = ocr_status()
            if ok:
                try:
                    for i, text in ocr_pdf(path, empty, env).items():
                        pages[i] = text
                    out["ocr"] = {"engine": "vision", "pages": [i + 1 for i in empty]}
                except Exception as exc:  # noqa: BLE001
                    out["ocr"] = {"error": str(exc)[:200]}
            else:
                out["ocr"] = {"available": False, "hint": hint}
        parts = []
        for n, p in enumerate(pages, 1):
            parts.append(f"## Page {n}")
            parts.append(p)
        out["text"], out["truncated"] = _cap(parts, max_chars)
        if total > len(pages):
            out["truncated"] = True
        return out
    if fmt == "docx":
        parts, reader = _docx_text(path, tools)
        out["reader"] = reader
    elif fmt == "xlsx":
        parts, total, read = _xlsx_text(path, max_pages, max_chars)
        out.update(sheets_total=total, sheets_read=read, reader="openpyxl")
    elif fmt == "pptx":
        parts, total, read = _pptx_text(path, max_pages)
        out.update(slides_total=total, slides_read=read, reader="python-pptx")
    elif fmt == "textutil":
        parts, out["reader"] = [_textutil(path, tools)], "textutil"
    elif fmt == "text":
        parts, out["reader"] = [_plain_text(path, max_chars)], "plain"
    else:  # pragma: no cover - callers check the format first
        raise InvalidArgument(f"no text extractor for {fmt}")
    out["text"], out["truncated"] = _cap(parts, max_chars)
    if out.get("sheets_total", 0) > out.get("sheets_read", 0) or out.get("slides_total", 0) > out.get("slides_read", 0):
        out["truncated"] = True
    return out


# ------------------------------------------------------------ audio and video (ffmpeg)


def probe(path: Path, tools: Tools) -> dict:
    ffprobe = tools.need("ffprobe", INSTALL_FFMPEG)
    proc = tools.run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                      "-of", "json", str(path)], timeout=60)
    if proc.returncode != 0:
        raise InvalidArgument("ffprobe could not read the file", detail=(proc.stderr or "")[:200])
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        data = {}
    kinds = {s.get("codec_type") for s in data.get("streams") or []}
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return {"duration": duration, "has_audio": "audio" in kinds, "has_video": "video" in kinds}


def video_frames(path: Path, count: int, duration: float | None, tools: Tools, tmp: Path) -> list[tuple[float, Path]]:
    """`count` frames spread over the video (the middle of each equal slice)."""
    ffmpeg = tools.need("ffmpeg", INSTALL_FFMPEG)
    dur = duration if duration and duration > 0 else 0.0
    stamps = [dur * (i + 0.5) / count for i in range(count)] if dur else [0.0]
    out = []
    for i, t in enumerate(stamps):
        target = tmp / f"frame-{i}.jpg"
        proc = tools.run([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"scale='min({MAX_SIDE},iw)':-2", "-y", str(target)], timeout=120)
        if proc.returncode == 0 and target.exists() and target.stat().st_size:
            out.append((round(t, 1), target))
    return out


def to_wav16k(path: Path, tools: Tools, out: Path) -> Path:
    ffmpeg = tools.need("ffmpeg", INSTALL_FFMPEG)
    proc = tools.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                      "-y", str(out)], timeout=600)
    if proc.returncode != 0 or not out.exists():
        raise InvalidArgument("ffmpeg could not extract the audio", detail=(proc.stderr or "")[:200])
    return out


def to_voice_note(path: Path, tools: Tools, out: Path) -> Path:
    """ogg/opus mono 48 kHz, the format WhatsApp plays as a voice note."""
    ffmpeg = tools.need("ffmpeg", INSTALL_FFMPEG)
    proc = tools.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000",
                      "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-y", str(out)], timeout=600)
    if proc.returncode != 0 or not out.exists():
        raise InvalidArgument("ffmpeg could not convert the audio to ogg/opus", detail=(proc.stderr or "")[:200])
    return out
