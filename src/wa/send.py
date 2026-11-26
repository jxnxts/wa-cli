"""Sending messages: a draft by default; a real send only with double confirmation.

A real send needs both:
1. `confirm=True` (on the CLI, `--confirm`);
2. `WA_ALLOW_SEND=1` in the process environment.

Methods:
- `ui` (default): opens `whatsapp://send?phone=...&text=...`, brings WhatsApp to the
  front, presses Return **only if the frontmost process is "WhatsApp"** and confirms
  in the database (read-only) that the message showed up. Needs Accessibility.
- `shortcut`: runs a Shortcuts app shortcut (default "WA Send", change it with
  `WA_SHORTCUT_NAME`) with the JSON {"to": "+1...", "text": "..."} as input.
- `draft`: never sends; only opens the chat with the text filled in.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Callable, Mapping
from urllib.parse import quote

from .core import WhatsApp, phone_digits
from .db import (
    APPLE_EPOCH_OFFSET,
    AccessibilityError,
    AutomationError,
    InvalidArgument,
    SendFailed,
    SendRefused,
)

MAX_SEND_CHARS = 4096
DEFAULT_SHORTCUT = "WA Send"
METHODS = ("ui", "shortcut", "draft")
DEFAULT_METHOD = "ui"
APP_PROCESS = "WhatsApp"

# Timings of the ui method (seconds).
UI_LOAD_DELAY = 3.0  # after opening the URL, so the text lands in the compose box
UI_FRONT_TIMEOUT = 5.0  # waiting for WhatsApp to become frontmost
UI_CONFIRM_TIMEOUT = 10.0  # waiting for the message to show up in the database
UI_POLL = 0.5

Runner = Callable[..., subprocess.CompletedProcess]

ACCESSIBILITY_MESSAGE = (
    "No Accessibility permission to press Return in WhatsApp. Grant "
    "Accessibility (System Settings > Privacy & Security > Accessibility) "
    "to the process that runs `wa`: the terminal app or the app/daemon that starts the "
    "MCP server (for example Claude Desktop or Cursor). Then reopen that app. Nothing was sent."
)
AUTOMATION_MESSAGE = (
    "No Automation permission to control WhatsApp/System Events. In System "
    "Settings > Privacy & Security > Automation, allow the process that runs "
    "`wa` to control \"System Events\" and \"WhatsApp\". Nothing was sent."
)

_FRONT_SCRIPT = (
    'tell application "System Events" to get name of first application process whose frontmost is true'
)
# Check the frontmost app and press Return in the SAME script, so there is no race.
_PRESS_SCRIPT = f"""tell application "System Events"
  set fp to name of first application process whose frontmost is true
  if fp is "{APP_PROCESS}" then
    key code 36
    return "pressed"
  else
    return "front:" & fp
  end if
end tell"""


def draft_url(phone: str, text: str) -> str:
    return f"whatsapp://send?phone={phone_digits(phone)}&text={quote(text, safe='')}"


def shortcut_name(env: Mapping[str, str]) -> str:
    return env.get("WA_SHORTCUT_NAME") or DEFAULT_SHORTCUT


def send_allowed(env: Mapping[str, str]) -> bool:
    return env.get("WA_ALLOW_SEND") == "1"


def validate_text(text: str | None, max_chars: int = MAX_SEND_CHARS) -> str:
    if text is None or not str(text).strip():
        raise InvalidArgument("the message text is empty")
    text = str(text)
    if len(text) > max_chars:
        raise InvalidArgument(f"text too long ({len(text)} characters; maximum {max_chars})")
    return text


# ------------------------------------------------------------ osascript


def _classify_osascript_error(stderr: str) -> None:
    # macOS may return these errors localized; the Portuguese fragments are matched on purpose.
    low = (stderr or "").lower()
    if (
        "assistive access" in low
        or "not allowed to send keystrokes" in low
        or "-25211" in low
        or "-1719" in low
        or "(1002)" in low
        or "acessibilidade" in low
    ):
        raise AccessibilityError(ACCESSIBILITY_MESSAGE, detail=(stderr or "").strip()[:300])
    if "-1743" in low or "not authorized to send apple events" in low or "não autorizado" in low:
        raise AutomationError(AUTOMATION_MESSAGE, detail=(stderr or "").strip()[:300])


def osascript(runner: Runner, script: str, timeout: float = 15) -> str:
    try:
        proc = runner(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SendFailed("osascript did not respond (timeout). Nothing was sent.") from None
    if proc.returncode != 0:
        _classify_osascript_error(proc.stderr)
        raise SendFailed("osascript failed.", stderr=(proc.stderr or "").strip()[:300])
    return (proc.stdout or "").strip()


# ------------------------------------------------------------ methods


def _send_ui(
    wa: WhatsApp,
    recipient: dict,
    text: str,
    result: dict,
    runner: Runner,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict:
    started = clock()
    since_apple = started - APPLE_EPOCH_OFFSET - 2  # slack for clock skew/rounding

    proc = runner(["open", draft_url(recipient["phone"], text)], capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise SendFailed("Could not open the WhatsApp link.", stderr=(proc.stderr or "").strip()[:300])
    sleep(UI_LOAD_DELAY)

    # Bring it to the front and wait up to UI_FRONT_TIMEOUT.
    osascript(runner, f'tell application "{APP_PROCESS}" to activate')
    deadline = clock() + UI_FRONT_TIMEOUT
    front = osascript(runner, _FRONT_SCRIPT)
    while front != APP_PROCESS and clock() < deadline:
        sleep(UI_POLL)
        front = osascript(runner, _FRONT_SCRIPT)
    if front != APP_PROCESS:
        result.update(
            {
                "sent": False,
                "mode": "not_sent",
                "reason": "whatsapp_not_frontmost",
                "pressed_return": False,
                "note": "WhatsApp did not become frontmost; Return was not pressed. Nothing was sent.",
            }
        )
        return result

    out = osascript(runner, _PRESS_SCRIPT)
    if out != "pressed":
        result.update(
            {
                "sent": False,
                "mode": "not_sent",
                "reason": "whatsapp_not_frontmost",
                "pressed_return": False,
                "note": "Another app came to the front before Return; nothing was sent.",
            }
        )
        return result

    # Confirm in the database (read-only).
    jids = [recipient["jid"], *wa.jids_for_phone(phone_digits(recipient["phone"]))]
    jids = list(dict.fromkeys(j for j in jids if j))
    deadline = clock() + UI_CONFIRM_TIMEOUT
    found = wa.find_sent_message(jids, text, since_apple)
    while found is None and clock() < deadline:
        sleep(UI_POLL)
        found = wa.find_sent_message(jids, text, since_apple)
    if found is None:
        result.update(
            {
                "sent": False,
                "mode": "not_sent",
                "reason": "not_confirmed_in_db",
                "pressed_return": True,
                "note": (
                    "Return was pressed, but the message did not show up in the database within "
                    f"{int(UI_CONFIRM_TIMEOUT)} s. Check WhatsApp before trying again "
                    "(so you don't send it twice)."
                ),
            }
        )
        return result
    result.update({"sent": True, "mode": "sent", "pressed_return": True, "confirmation": found})
    return result


def _send_shortcut(result: dict, recipient: dict, text: str, env: Mapping[str, str], runner: Runner) -> dict:
    name = shortcut_name(env)
    listed = runner(["shortcuts", "list"], capture_output=True, text=True, timeout=30)
    names = {line.strip() for line in (listed.stdout or "").splitlines()}
    if listed.returncode != 0 or name not in names:
        raise SendFailed(
            f"Shortcut {name!r} not found in the Shortcuts app. Use --method ui (default) or create the shortcut.",
            draft=result,
        )
    payload = json.dumps({"to": recipient["phone"], "text": text}, ensure_ascii=False)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(payload)
        tmp = fh.name
    try:
        proc = runner(
            ["shortcuts", "run", name, "--input-path", tmp],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise SendFailed("The shortcut took too long to respond (timeout).", draft=result) from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        raise SendFailed(
            "The shortcut failed.",
            stderr=(proc.stderr or "").strip()[:500],
            returncode=proc.returncode,
            draft=result,
        )
    result.update(
        {
            "sent": True,
            "mode": "sent",
            "shortcut": name,
            "note": "The shortcut ran without errors. Check WhatsApp to see whether the message went out.",
        }
    )
    return result


def send_message(
    wa: WhatsApp,
    to: str,
    text: str,
    confirm: bool = False,
    method: str = DEFAULT_METHOD,
    open_draft: bool = False,
    env: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    max_chars: int = MAX_SEND_CHARS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict:
    env = os.environ if env is None else env
    if method not in METHODS:
        raise InvalidArgument(f"invalid method: {method!r} (use {', '.join(METHODS)})")
    text = validate_text(text, max_chars)
    recipient = wa.resolve_recipient(to)
    result: dict = {
        "sent": False,
        "mode": "draft",
        "method": method,
        "to": recipient,
        "text": text,
        "chars": len(text),
    }

    if not confirm or method == "draft":
        if confirm and method == "draft":
            result["note"] = "--method draft never sends; it only opens the chat with the text filled in."
        if method == "draft" and open_draft:
            proc = runner(["open", draft_url(recipient["phone"], text)], capture_output=True, text=True, timeout=15)
            result["draft_opened"] = proc.returncode == 0
            if proc.returncode != 0:
                result["draft_error"] = (proc.stderr or "").strip()[:500]
        result["hint"] = (
            "Nothing was sent. To really send: confirm=true (CLI: --confirm) "
            "with WA_ALLOW_SEND=1 in the environment."
        )
        result["send_allowed_by_env"] = send_allowed(env)
        return result

    if not send_allowed(env):
        raise SendRefused(
            "Send refused: set WA_ALLOW_SEND=1 in the process environment to allow sending.",
            draft=result,
        )

    if method == "ui":
        return _send_ui(wa, recipient, text, result, runner, sleep, clock)
    return _send_shortcut(result, recipient, text, env, runner)
