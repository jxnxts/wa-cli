"""Build the README animation (docs/img/wa-flow.gif) from scene.html.

The scene is an HTML page animated by `window.render(t)` (t from 0 to 1). This
script downloads the logos, builds the page, captures every frame with the
Playwright Chromium and encodes the GIF with ffmpeg.

Needs Playwright 1.49.1 with its Chromium, and ffmpeg (brew install ffmpeg).
From the repository root:

    uv run --no-project --with playwright==1.49.1 playwright install chromium   # first time only
    uv run --no-project --with playwright==1.49.1 python -I scripts/flow-gif/build.py

usage: build.py [output.gif] [fps] [seconds]
       defaults: docs/img/wa-flow.gif, 20 fps, 10 s (200 frames), 1200x675

GIF settings: frames captured at 2x (2400x1350) and scaled to 1200x675 (lanczos);
a 256-color palette from palettegen stats_mode=full; paletteuse with sierra2_4a
dithering and diff_mode=rectangle; loop 0 (repeats forever). ffmpeg comes from
the FFMPEG environment variable or the PATH.

Logos: downloaded on every run, from pinned versions, into a temporary folder
(they are not stored in the repository):
  - simple-icons 16.37.0: whatsapp
  - @lobehub/icons-static-svg 1.102.0: grokbot, claudecode-color, codex-color,
    cursor, geminicli-color, opencode
They are trademarks of their owners and appear only to show compatibility.

Fonts: Geist and JetBrains Mono when they are in ~/Library/Fonts or /Library/Fonts.
Without them the scene falls back to the system fonts (system-ui and ui-monospace).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "docs" / "img" / "wa-flow.gif"
FPS = int(sys.argv[2]) if len(sys.argv) > 2 else 20
SECS = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
N = round(FPS * SECS)
W, H = 1200, 675
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg")

SIMPLE_ICONS = "https://cdn.jsdelivr.net/npm/simple-icons@16.37.0/icons/{}.svg"
LOBE_ICONS = "https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@1.102.0/icons/{}.svg"
LOGO_URLS = {"si-whatsapp": SIMPLE_ICONS.format("whatsapp")}
for _name in ["grokbot", "claudecode-color", "codex-color", "cursor", "geminicli-color", "opencode"]:
    LOGO_URLS[f"lobe-{_name}"] = LOBE_ICONS.format(_name)

FONT_DIRS = [Path.home() / "Library" / "Fonts", Path("/Library/Fonts")]
# (family used in the scene, file name, CSS weight)
FONTS = [
    ("Geist", "Geist[wght].ttf", "100 900"),
    ("JBM", "JetBrainsMono-Regular.ttf", "400"),
    ("JBM", "JetBrainsMono-Medium.ttf", "500"),
    ("JBM", "JetBrainsMono-Bold.ttf", "700"),
]


def download_logos(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, url in LOGO_URLS.items():
        req = urllib.request.Request(url, headers={"User-Agent": "wa-flow-gif"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https URL)
            data = resp.read().decode("utf-8")
        if "<svg" not in data:
            sys.exit(f"error: {url} did not return an SVG")
        (folder / f"{name}.svg").write_text(data)
    return folder


def font_faces() -> str:
    """@font-face rules for the fonts that exist; missing ones fall back to the CSS defaults."""
    rules, missing = [], []
    for family, filename, weight in FONTS:
        path = next((d / filename for d in FONT_DIRS if (d / filename).exists()), None)
        if path is None:
            missing.append(filename)
            continue
        rules.append(f'@font-face {{ font-family: {family}; src: url("{path.as_uri()}"); font-weight: {weight}; }}')
    if missing:
        print("fonts not found, using the system fonts:", ", ".join(missing), file=sys.stderr)
    return "\n".join(rules)


def logo(logos: Path, name: str, size: int, color: str | None = None) -> str:
    """Inline an SVG logo at a fixed square size, optionally tinting monochrome marks."""
    svg = (logos / f"{name}.svg").read_text()
    svg = re.sub(r"<title>.*?</title>", "", svg, flags=re.S)
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)
    svg = re.sub(r'\sstyle="[^"]*"', "", svg, count=1)
    # keep gradient ids unique when the same logo is inlined twice
    svg = svg.replace("lobe-icons-", f"l{size}-")
    extra = f' width="{size}" height="{size}" class="logo"'
    if color:
        extra += f' fill="{color}"'
    return svg.replace("<svg", "<svg" + extra, 1)


def build_page(logos: Path, target: Path) -> Path:
    html = (HERE / "scene.html").read_text()
    html = re.sub(r"/\* __FONTS__.*?\*/", lambda _: font_faces(), html, count=1)
    white = "#f7f8f8"
    subs = {
        "__WA_14__": logo(logos, "si-whatsapp", 14, "#25d366"),
        "__GROKBOT__": logo(logos, "lobe-grokbot", 24, white),
        "__GROKBOT_20__": logo(logos, "lobe-grokbot", 20, white),
        "__CLAUDECODE__": logo(logos, "lobe-claudecode-color", 24),
        "__CODEX__": logo(logos, "lobe-codex-color", 24),
        "__CURSOR__": logo(logos, "lobe-cursor", 24, white),
        "__GEMINICLI__": logo(logos, "lobe-geminicli-color", 24),
        "__OPENCODE__": logo(logos, "lobe-opencode", 22, white),
    }
    for k, v in subs.items():
        html = html.replace(k, v)
    target.write_text(html)
    return target


def capture(page_file: Path, frames: Path) -> None:
    frames.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
        pg.goto(page_file.as_uri())
        pg.evaluate("document.fonts.ready")
        pg.wait_for_timeout(400)
        for f in range(N):
            pg.evaluate(f"window.render({f / N})")
            pg.screenshot(path=str(frames / f"f{f:04d}.png"))
        browser.close()


def ff(*args) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True)


def encode(frames: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    src = ["-framerate", str(FPS), "-i", str(frames / "f%04d.png")]
    pal = str(frames / "palette.png")
    scale = f"scale={W}:{H}:flags=lanczos"
    ff(*src, "-vf", f"{scale},palettegen=max_colors=256:stats_mode=full", pal)
    ff(*src, "-i", pal, "-lavfi",
       f"{scale}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
       "-loop", "0", str(out))


def main() -> None:
    if not FFMPEG:
        sys.exit("error: ffmpeg not found (brew install ffmpeg, or set FFMPEG)")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        logos = download_logos(tmp / "logos")
        page = build_page(logos, tmp / "scene.built.html")
        capture(page, tmp / "frames")
        encode(tmp / "frames", OUT)
    print(f"{N} frames at {FPS} fps = {SECS:g} s -> {OUT} ({OUT.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
