"""The agent skills in skills/: valid frontmatter, and a kanban template with no external requests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
SKILL_DIRS = sorted(p for p in SKILLS.iterdir() if p.is_dir()) if SKILLS.is_dir() else []
KANBAN = SKILLS / "whatsapp-war-room" / "assets" / "kanban-template.html"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter(text: str) -> dict[str, str]:
    """The `key: value` lines between the opening and closing `---`."""
    lines = text.splitlines()
    assert lines and lines[0] == "---", "SKILL.md must start with ---"
    end = lines.index("---", 1)
    out = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        assert sep and key == key.strip() and key, f"bad frontmatter line: {line!r}"
        out[key] = value.strip()
    return out


def test_the_four_skills_are_there():
    assert [p.name for p in SKILL_DIRS] == [
        "whatsapp-lists", "whatsapp-mac", "whatsapp-routines", "whatsapp-war-room",
    ]


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_frontmatter(skill: Path):
    meta = frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
    assert meta.get("name") == skill.name
    assert NAME_RE.match(meta["name"]) and len(meta["name"]) <= 64
    description = meta.get("description", "")
    assert description, "description is missing"
    assert len(description) <= 1024
    assert "Use when" in description
    # a plain YAML scalar: no ": " or " #" inside, and no leading quote or indicator
    assert ": " not in description and " #" not in description
    assert description[0] not in "\"'[{>|*&!%@`"


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_text_has_no_em_dash(skill: Path):
    for path in skill.rglob("*"):
        if path.is_file():
            assert "—" not in path.read_text(encoding="utf-8"), path


def test_kanban_template_makes_no_external_requests():
    html = KANBAN.read_text(encoding="utf-8")
    urls = set(re.findall(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[^\s\"')]*", html))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls
    assert "default-src 'none'" in html
    for pattern in (r"\bfetch\(", r"XMLHttpRequest", r"WebSocket", r"sendBeacon", r"<link\b", r"@import", r"\bsrc=\""):
        assert not re.search(pattern, html), pattern


def test_kanban_template_data_matches_the_skill():
    html = KANBAN.read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
    data = json.loads(raw)
    assert [s[0] for s in data["status"]] == ["waiting_on_you", "you_promised", "waiting_on_them", "fyi", "done"]
    assert [p[0] for p in data["prio"]] == ["high", "medium", "low"]
    for status, _label in data["status"]:
        assert f"--c-{status}:" in html
    for prio, _label in data["prio"]:
        assert f".badge.{prio}" in html
