"""Sending messages: a draft by default; a real send only with double confirmation.

A real send needs both:
1. `confirm=True` (on the CLI, `--confirm`);
2. `WA_ALLOW_SEND=1` in the process environment.

Recipients: people (phone number, @s.whatsapp.net, @lid, address book name) and groups
(`…@g.us`, which must be among the known chats). Broadcast lists (@broadcast) and
channels (@newsletter) are refused.

Methods:
- `ui` (default): first checks (through `ioreg`, without System Events) that the Mac
  screen is not locked; then opens the chat, brings WhatsApp to the front, presses
  Return **only if the frontmost process is "WhatsApp"** and confirms in the database
  (read-only) that the message showed up. Needs Accessibility.
  - person: opens `whatsapp://send?phone=<digits>&text=<text>` (the chat with the
    text already in the compose box).
  - group: opens `whatsapp://openchat/<jid@g.us>`, WhatsApp's own deep link to open a
    chat by JID (it takes no text). The text goes through the clipboard: the current
    text content is saved, the message text is copied, Cmd+A and Cmd+V go to the
    compose box (only with WhatsApp in front), then Return, and the clipboard gets its
    previous text back.
- `shortcut`: runs a Shortcuts app shortcut (default "WA Send", change it with
  `WA_SHORTCUT_NAME`) with the JSON {"to": "+1...", "text": "..."} as input.
  People only (the shortcut takes a phone number).
- `draft`: never sends; only opens the chat (with the text filled in, for a person).
"""

from __future__ import annotations

import json
import os
import re
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
    ScreenLockedError,
    SendFailed,
    SendRefused,
    WAError,
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
UI_PASTE_DELAY = 0.5  # group: after pasting, before Return
OSASCRIPT_TIMEOUT = 15.0
IOREG_TIMEOUT = 5.0

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

SCREEN_LOCKED_MESSAGE = (
    "The Mac screen is locked; sending through the app needs an unlocked session. Nothing was sent."
)
SYSTEM_EVENTS_TIMEOUT_MESSAGE = (
    "System Events did not answer osascript within {timeout:g} s (timeout). Common causes: the Mac "
    "screen is locked (System Events does not answer on the lock screen), the process that runs `wa` "
    "lacks Accessibility/Automation permission, or WhatsApp is not in front or is waiting on a dialog. "
    "{outcome}"
)
APP_TIMEOUT_MESSAGE = (
    "WhatsApp did not answer osascript within {timeout:g} s (timeout). Check that the app is open "
    "and has no pending dialog. {outcome}"
)
NOTHING_SENT = "Nothing was sent."

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


# Group: paste the text (Cmd+A, Cmd+V) into the compose box, only with WhatsApp in front.
_PASTE_SCRIPT = f"""-- wa:paste
tell application "System Events"
  set fp to name of first application process whose frontmost is true
  if fp is "{APP_PROCESS}" then
    keystroke "a" using command down
    keystroke "v" using command down
    return "pasted"
  else
    return "front:" & fp
  end if
end tell"""


def draft_url(phone: str, text: str) -> str:
    return f"whatsapp://send?phone={phone_digits(phone)}&text={quote(text, safe='')}"


def group_chat_url(jid: str) -> str:
    """Opens the chat by JID (WhatsApp's `openchat` deep link); it does not fill in text."""
    return f"whatsapp://openchat/{quote(jid, safe='@.')}"


def is_group(recipient: Mapping) -> bool:
    return recipient.get("kind") == "group"


def open_url_for(recipient: Mapping, text: str) -> str:
    if is_group(recipient):
        return group_chat_url(recipient["jid"])
    return draft_url(recipient["phone"], text)


# ------------------------------------------------------------ locked screen

IOREG_CMD = ["ioreg", "-n", "Root", "-d1"]
_ROOT_LOCKED = re.compile(r'"IOConsoleLocked"\s*=\s*Yes')
_SESSION = re.compile(r"\{[^{}]*\}")
_SESSION_ON_CONSOLE = re.compile(r'"kCGSSessionOnConsoleKey"\s*=\s*Yes')
_SESSION_LOCKED = re.compile(r'"CGSSessionScreenIsLocked"\s*=\s*Yes')


def parse_screen_locked(ioreg_out: str) -> bool:
    """Reads `ioreg -n Root -d1` output: locked if `IOConsoleLocked = Yes` or if the console
    session has `CGSSessionScreenIsLocked=Yes` (the key goes away when the screen unlocks)."""
    out = ioreg_out or ""
    if _ROOT_LOCKED.search(out):
        return True
    for session in _SESSION.findall(out):
        if _SESSION_ON_CONSOLE.search(session) and _SESSION_LOCKED.search(session):
            return True
    return False


def screen_locked(runner: Runner = subprocess.run) -> bool | None:
    """True/False from the `ioreg` output (no System Events); None when it cannot tell."""
    try:
        proc = runner(IOREG_CMD, capture_output=True, text=True, timeout=IOREG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    return parse_screen_locked(proc.stdout)


def ensure_screen_unlocked(runner: Runner = subprocess.run, message: str = SCREEN_LOCKED_MESSAGE) -> None:
    """Fails fast (before any Apple Event to System Events) when the screen is locked.
    If `ioreg` does not answer, it goes on: the osascript timeout covers that case."""
    if screen_locked(runner):
        raise ScreenLockedError(message, reason="screen_locked")


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


def osascript(
    runner: Runner,
    script: str,
    timeout: float = OSASCRIPT_TIMEOUT,
    *,
    outcome: str = NOTHING_SENT,
    error_cls: type[WAError] = SendFailed,
) -> str:
    try:
        proc = runner(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        template = SYSTEM_EVENTS_TIMEOUT_MESSAGE if "System Events" in script else APP_TIMEOUT_MESSAGE
        raise error_cls(template.format(timeout=timeout, outcome=outcome), reason="osascript_timeout") from None
    if proc.returncode != 0:
        _classify_osascript_error(proc.stderr)
        raise error_cls("osascript failed.", stderr=(proc.stderr or "").strip()[:300])
    return (proc.stdout or "").strip()


# ------------------------------------------------------------ methods


_UTF8_ENV = {"LANG": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"}


def _clipboard_get(runner: Runner) -> str | None:
    """Current clipboard text (None when it cannot be read)."""
    try:
        proc = runner(["pbpaste"], capture_output=True, text=True, timeout=5, env={**os.environ, **_UTF8_ENV})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _clipboard_set(runner: Runner, text: str) -> bool:
    try:
        proc = runner(["pbcopy"], input=text, capture_output=True, text=True, timeout=5, env={**os.environ, **_UTF8_ENV})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _not_sent(result: dict, reason: str, note: str, pressed: bool = False) -> dict:
    result.update({"sent": False, "mode": "not_sent", "reason": reason, "pressed_return": pressed, "note": note})
    return result


def _confirm_jids(wa: WhatsApp, recipient: dict) -> list[str]:
    if is_group(recipient):
        return [recipient["jid"]]  # a group has no phone number: check by the group JID only
    jids = [recipient["jid"], *wa.jids_for_phone(phone_digits(recipient["phone"]))]
    return list(dict.fromkeys(j for j in jids if j))


def _send_ui(
    wa: WhatsApp,
    recipient: dict,
    text: str,
    result: dict,
    runner: Runner,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict:
    # Locked screen: System Events does not answer and everything times out. Fail fast.
    ensure_screen_unlocked(runner)

    started = clock()
    since_apple = started - APPLE_EPOCH_OFFSET - 2  # slack for clock skew/rounding
    group = is_group(recipient)
    jids = _confirm_jids(wa, recipient)

    saved_clipboard: str | None = None
    try:
        proc = runner(["open", open_url_for(recipient, text)], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise SendFailed("Could not open the WhatsApp link. Nothing was sent.", stderr=(proc.stderr or "").strip()[:300])
        sleep(UI_LOAD_DELAY)

        # Bring it to the front and wait up to UI_FRONT_TIMEOUT.
        osascript(runner, f'tell application "{APP_PROCESS}" to activate')
        deadline = clock() + UI_FRONT_TIMEOUT
        front = osascript(runner, _FRONT_SCRIPT)
        while front != APP_PROCESS and clock() < deadline:
            sleep(UI_POLL)
            front = osascript(runner, _FRONT_SCRIPT)
        if front != APP_PROCESS:
            return _not_sent(
                result, "whatsapp_not_frontmost",
                "WhatsApp did not become frontmost; Return was not pressed. Nothing was sent.",
            )

        if group:
            # Copy only now, with WhatsApp in front: anything the user copied during the waits
            # above would otherwise be pasted and sent instead.
            saved_clipboard = _clipboard_get(runner)
            if not _clipboard_set(runner, text):
                raise SendFailed("Could not copy the text to the clipboard (pbcopy). Nothing was sent.")
            if osascript(runner, _PASTE_SCRIPT) != "pasted":
                return _not_sent(
                    result, "whatsapp_not_frontmost",
                    "Another app came to the front before the paste; nothing was pasted or sent.",
                )
            sleep(UI_PASTE_DELAY)

        try:
            out = osascript(runner, _PRESS_SCRIPT)
        except SendFailed as exc:
            if exc.details.get("reason") != "osascript_timeout":
                raise
            # Only claim "nothing was sent" after looking at the database.
            if wa.find_sent_message(jids, text, since_apple) is None:
                raise
            out = "pressed"
        if out != "pressed":
            return _not_sent(result, "whatsapp_not_frontmost", "Another app came to the front before Return; nothing was sent.")
    finally:
        if saved_clipboard is not None:
            result["clipboard_restored"] = _clipboard_set(runner, saved_clipboard)

    # Confirm in the database (read-only).
    deadline = clock() + UI_CONFIRM_TIMEOUT
    found = wa.find_sent_message(jids, text, since_apple)
    while found is None and clock() < deadline:
        sleep(UI_POLL)
        found = wa.find_sent_message(jids, text, since_apple)
    if found is None:
        return _not_sent(
            result, "not_confirmed_in_db",
            "Return was pressed, but the message did not show up in the database within "
            f"{int(UI_CONFIRM_TIMEOUT)} s. Check WhatsApp before trying again "
            "(so you don't send it twice).",
            pressed=True,
        )
    result.update({"sent": True, "mode": "sent", "pressed_return": True, "confirmation": found})
    if group and not found.get("chat_matched"):
        result["warning"] = (
            f"The message showed up in chat {found.get('chat_jid')}, not in group {recipient['jid']}. "
            "Check WhatsApp."
        )
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
    if method == "shortcut" and is_group(recipient):
        raise InvalidArgument(
            "method 'shortcut' only sends to people (the shortcut takes a phone number); "
            "for a group use method 'ui' (the default)"
        )
    result: dict = {
        "sent": False,
        "mode": "draft",
        "method": method,
        "to": recipient,
        "text": text,
        "chars": len(text),
        "open_url": open_url_for(recipient, text),
    }
    if is_group(recipient) and method != "shortcut":
        result["group_note"] = (
            "Group: the chat opens through whatsapp://openchat/<jid> (the link carries no text); "
            "on a real send the text goes through the clipboard (Cmd+V) and the clipboard gets its previous text back."
        )

    if not confirm or method == "draft":
        if confirm and method == "draft":
            result["note"] = "--method draft never sends; it only opens the chat" + (
                " (for a group, without the text: the openchat link carries no text)."
                if is_group(recipient) else " with the text filled in."
            )
        if method == "draft" and open_draft:
            proc = runner(["open", open_url_for(recipient, text)], capture_output=True, text=True, timeout=15)
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
