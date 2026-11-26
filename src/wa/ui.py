"""WhatsApp UI automation through System Events (menu bar).

WhatsApp menu titles start with the invisible character U+200E (LRM); every
comparison is made without it. Scripts reference menus and items by position,
after reading the titles. Each script starts with an `-- wa:<op> ...` comment
(ignored by AppleScript) that names the operation; the tests use that marker to
simulate the app.

Titles are looked up in every language listed by the caller (see `actions.py`),
so the same code works with WhatsApp in Portuguese or English.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from .db import ActionFailed
from .send import APP_PROCESS, _FRONT_SCRIPT, osascript

Runner = Callable[..., subprocess.CompletedProcess]

FRONT_TIMEOUT = 5.0
POLL = 0.5
LRM = "‎"


def clean(title: str | None) -> str:
    if title is None:
        return ""
    return title.replace(LRM, "").replace("‏", "").strip()


def norm_title(title: str | None) -> str:
    """Without LRM, lowercase, without a trailing ellipsis (… or ...)."""
    t = clean(title).casefold()
    for suffix in ("…", "..."):
        if t.endswith(suffix):
            t = t[: -len(suffix)].rstrip()
    return t


def _menu_ref(path: list[int]) -> str:
    """path [i] -> menu of bar item i; [i, j] -> submenu of item j; and so on."""
    ref = f"menu 1 of menu bar item {path[0]} of menu bar 1"
    for idx in path[1:]:
        ref = f"menu 1 of menu item {idx} of {ref}"
    return ref


def _item_ref(path: list[int]) -> str:
    """path [i, j(, k...)] -> item j (of submenu ...)."""
    ref = f"menu 1 of menu bar item {path[0]} of menu bar 1"
    for idx in path[1:-1]:
        ref = f"menu 1 of menu item {idx} of {ref}"
    return f"menu item {path[-1]} of {ref}"


_LIST_BODY = """  set out to {{}}
  repeat with mi in {items}
    set n to ""
    try
      set n to (name of mi) as text
    end try
    set e to "false"
    try
      set e to (enabled of mi) as text
    end try
    set end of out to n & tab & e
  end repeat
  set AppleScript's text item delimiters to linefeed
  return out as text"""


def script_list_bar() -> str:
    body = _LIST_BODY.format(items="menu bar items of menu bar 1")
    return f'-- wa:list-bar\ntell application "System Events" to tell process "{APP_PROCESS}"\n{body}\nend tell'


def script_list_items(path: list[int]) -> str:
    body = _LIST_BODY.format(items=f"menu items of {_menu_ref(path)}")
    marker = ",".join(map(str, path))
    return f'-- wa:list-items {marker}\ntell application "System Events" to tell process "{APP_PROCESS}"\n{body}\nend tell'


def script_click(path: list[int]) -> str:
    """Click the item only if WhatsApp is frontmost (check and click in the same script)."""
    marker = ",".join(map(str, path))
    return (
        f"-- wa:click {marker}\n"
        'tell application "System Events"\n'
        "  set fp to name of first application process whose frontmost is true\n"
        f'  if fp is not "{APP_PROCESS}" then return "front:" & fp\n'
        f'  tell process "{APP_PROCESS}"\n'
        f"    click {_item_ref(path)}\n"
        "  end tell\n"
        '  return "clicked"\n'
        "end tell"
    )


def _parse_items(out: str) -> list[tuple[str, bool]]:
    items = []
    for line in (out or "").splitlines():
        name, _, enabled = line.partition("\t")
        if name == "missing value":
            name = ""
        items.append((name, enabled.strip() == "true"))
    return items


@dataclass
class MenuItem:
    path: list[int]
    title: str  # without LRM
    enabled: bool


@dataclass
class WhatsAppUI:
    runner: Runner = subprocess.run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    log: list[str] = field(default_factory=list)

    def osa(self, script: str) -> str:
        return osascript(self.runner, script)

    def open_url(self, url: str) -> None:
        proc = self.runner(["open", url], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise ActionFailed("Could not open the WhatsApp link.", stderr=(proc.stderr or "").strip()[:300])
        self.log.append("open_url")

    def bring_to_front(self, timeout: float = FRONT_TIMEOUT) -> bool:
        self.osa(f'tell application "{APP_PROCESS}" to activate')
        deadline = self.clock() + timeout
        front = self.osa(_FRONT_SCRIPT)
        while front != APP_PROCESS and self.clock() < deadline:
            self.sleep(POLL)
            front = self.osa(_FRONT_SCRIPT)
        self.log.append(f"front={front == APP_PROCESS}")
        return front == APP_PROCESS

    def bar(self) -> list[MenuItem]:
        return [MenuItem([i], clean(n), e) for i, (n, e) in enumerate(_parse_items(self.osa(script_list_bar())), 1)]

    def find_bar(self, titles: str | list[str] | tuple[str, ...]) -> MenuItem | None:
        """Bar menu whose title matches any of `titles` (one per UI language)."""
        if isinstance(titles, str):
            titles = (titles,)
        wanted = [norm_title(t) for t in titles]
        return next((m for m in self.bar() if norm_title(m.title) in wanted), None)

    def items(self, path: list[int]) -> list[MenuItem]:
        out = self.osa(script_list_items(path))
        return [MenuItem([*path, j], clean(n), e) for j, (n, e) in enumerate(_parse_items(out), 1) if n]

    def find_item(self, path: list[int], titles: list[str] | tuple[str, ...]) -> MenuItem | None:
        wanted = [norm_title(t) for t in titles]
        for m in self.items(path):
            if norm_title(m.title) in wanted:
                return m
        return None

    def find_item_containing(self, path: list[int], fragment: str) -> MenuItem | None:
        frag = norm_title(fragment)
        return next((m for m in self.items(path) if frag in norm_title(m.title)), None)

    def click(self, item: MenuItem) -> bool:
        out = self.osa(script_click(item.path))
        self.log.append(f"click:{item.title}:{out}")
        return out == "clicked"

    def wait_for(self, check: Callable[[], bool], timeout: float) -> bool:
        deadline = self.clock() + timeout
        ok = check()
        while not ok and self.clock() < deadline:
            self.sleep(POLL)
            ok = check()
        return ok
