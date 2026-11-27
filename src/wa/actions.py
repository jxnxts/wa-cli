"""Chat management actions and calls through the WhatsApp menu bar.

Nothing here writes to the databases: archive, pin, mute and so on sync with the
phone, so only the app's own UI can do them. The database is read (read-only) to
verify the result when there is a signal.

Guards:
- chat actions: `confirm=True` and `WA_ALLOW_ACTIONS=1`;
- calling and hanging up: `confirm=True` and `WA_ALLOW_CALLS=1`.

Menu titles depend on the language of the WhatsApp UI. Every title below is a
tuple with the Brazilian Portuguese title first and the English one second; the
code accepts any of them. The Portuguese titles were observed on a real account;
the English ones come from the app's own localization files
(WhatsApp.app/Contents/Resources/en.lproj and SharedModules.framework).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping

from .core import WhatsApp, phone_digits
from .db import ActionFailed, ActionRefused, InvalidArgument
from .send import UI_LOAD_DELAY
from .ui import WhatsAppUI, norm_title

CONFIRM_TIMEOUT = 10.0
SETTLE_DELAY = 0.8  # after the chat opens and the app comes to the front, before the 1st click
RETRY_DELAY = 1.0  # before the second (and last) attempt
CALL_START_TIMEOUT = 15.0
HANGUP_TIMEOUT = 5.0

# WhatsApp UI titles: (pt-BR, en). Matching ignores case, LRM and a trailing ellipsis.
MENU_CHAT = ("Conversa", "Chat")
MENU_CALL = ("Ligar", "Call")
ITEM_HANGUP = ("Encerrar ligação", "End Call")
ITEM_MUTE = ("Silenciar", "Mute")
ITEM_ARCHIVE = ("Arquivar", "Archive")
ITEM_UNARCHIVE = ("Desarquivar", "Unarchive")
ITEM_PIN = ("Fixar", "Pin")
ITEM_UNPIN = ("Desafixar", "Unpin")
ITEM_MARK_UNREAD = ("Marcar como não lida", "Mark as unread")
ITEM_MARK_READ = ("Marcar como lida", "Mark as read")
MUTE_8H = ("8 horas", "8 hours")
MUTE_1W = ("1 semana", "1 week")
MUTE_ALWAYS = ("Sempre", "Always")
SUBMENU_MICROPHONE = ("Microfone", "Microphone")
SUBMENU_SPEAKER = ("Alto-falante", "Speaker")
# The exact title of the item that unmutes a chat was not observed; accept these.
UNMUTE_TITLES = (
    "Reativar som",
    "Ativar som",
    "Reativar notificações",
    "Ativar notificações",
    "Desativar silêncio",
    "Dessilenciar",
    "Remover silêncio",
    "Tirar do silêncio",
    "Unmute",
    "Unmute sound",
    "Unmute notifications",
    "Turn on notifications",
)
CALL_ITEMS = {"voice": ("Ligação de voz", "Voice call"), "video": ("Ligação de vídeo", "Video call")}


def _en(titles: tuple[str, ...]) -> str:
    """Title used in plans and messages (the English one when there is one)."""
    return titles[1] if len(titles) > 1 else titles[0]


@dataclass(frozen=True)
class ActionSpec:
    items: tuple[str, ...]  # title(s) of the item to click
    already: tuple[str, ...]  # title(s) showing the chat is already in the requested state
    submenu: tuple[str, ...] = ()  # submenu item (Mute > 8 hours)
    check: Callable[[dict], bool] | None = None  # signal in the database
    signal: str | None = None


ACTIONS: dict[str, ActionSpec] = {
    "archive": ActionSpec(ITEM_ARCHIVE, ITEM_UNARCHIVE, check=lambda s: s["archived"], signal="ZARCHIVED"),
    "unarchive": ActionSpec(ITEM_UNARCHIVE, ITEM_ARCHIVE, check=lambda s: not s["archived"], signal="ZARCHIVED"),
    "pin": ActionSpec(ITEM_PIN, ITEM_UNPIN, check=lambda s: s["pinned"], signal="ZLASTMESSAGEDATE (pinned)"),
    "unpin": ActionSpec(ITEM_UNPIN, ITEM_PIN, check=lambda s: not s["pinned"], signal="ZLASTMESSAGEDATE (pinned)"),
    "mute-8h": ActionSpec(ITEM_MUTE, UNMUTE_TITLES, MUTE_8H, check=lambda s: s["muted"], signal="ZWACHATPROPERTIES.ZMUTEDATE"),
    "mute-1w": ActionSpec(ITEM_MUTE, UNMUTE_TITLES, MUTE_1W, check=lambda s: s["muted"], signal="ZWACHATPROPERTIES.ZMUTEDATE"),
    "mute-always": ActionSpec(ITEM_MUTE, UNMUTE_TITLES, MUTE_ALWAYS, check=lambda s: s["muted"], signal="ZWACHATPROPERTIES.ZMUTEDATE"),
    "unmute": ActionSpec(UNMUTE_TITLES, ITEM_MUTE, check=lambda s: not s["muted"], signal="ZWACHATPROPERTIES.ZMUTEDATE"),
    "mark-unread": ActionSpec(ITEM_MARK_UNREAD, ITEM_MARK_READ, check=lambda s: s["unread"] != 0, signal="ZUNREADCOUNT"),
}


def actions_allowed(env: Mapping[str, str]) -> bool:
    return env.get("WA_ALLOW_ACTIONS") == "1"


def calls_allowed(env: Mapping[str, str]) -> bool:
    return env.get("WA_ALLOW_CALLS") == "1"


def chat_url(phone: str) -> str:
    return f"whatsapp://send?phone={phone_digits(phone)}"


def _recipient(wa: WhatsApp, to: str) -> tuple[dict, list[str]]:
    t = (to or "").strip()
    if t.endswith("@g.us") or t.endswith("@broadcast") or t.endswith("@newsletter"):
        raise InvalidArgument(
            "Groups, broadcast lists and channels are not supported by desktop actions yet (they have "
            "no phone number to open the chat with). Use a one-to-one chat, or --via companion for groups."
        )
    rec = wa.resolve_recipient(t)
    jids = [rec["jid"], *wa.jids_for_phone(phone_digits(rec["phone"]))]
    return rec, list(dict.fromkeys(j for j in jids if j))


def _open_chat(ui: WhatsAppUI, phone: str) -> bool:
    ui.open_url(chat_url(phone))
    ui.sleep(UI_LOAD_DELAY)
    if not ui.bring_to_front():
        return False
    ui.sleep(SETTLE_DELAY)
    return True


def _chat_menu(ui: WhatsAppUI):
    bar = ui.find_bar(MENU_CHAT)
    if bar is None:
        raise ActionFailed(
            f"Menu {_en(MENU_CHAT)!r} not found in the WhatsApp menu bar (unknown UI language?). Nothing was done.",
            menu_items=[m.title for m in ui.bar() if m.title],
        )
    return bar


def call_active(ui: WhatsAppUI) -> bool:
    bar = ui.find_bar(MENU_CALL)
    if bar is None:
        return False
    item = ui.find_item(bar.path, ITEM_HANGUP)
    return bool(item and item.enabled)


# ------------------------------------------------------------ chat actions


def chat_action(
    wa: WhatsApp,
    to: str,
    action: str,
    confirm: bool = False,
    env: Mapping[str, str] | None = None,
    ui: WhatsAppUI | None = None,
) -> dict:
    env = os.environ if env is None else env
    if action not in ACTIONS:
        raise InvalidArgument(f"invalid action: {action!r} (use {', '.join(ACTIONS)})")
    spec = ACTIONS[action]
    rec, jids = _recipient(wa, to)
    before = wa.chat_state(jids)
    path = " > ".join([_en(MENU_CHAT), _en(spec.items), *([_en(spec.submenu)] if spec.submenu else [])])
    result: dict = {
        "action": action,
        "to": rec,
        "done": False,
        "mode": "plan",
        "state_before": before,
        "plan": [
            f"open {chat_url(rec['phone'])}",
            "bring WhatsApp to the front",
            f"read the item's current title ({_en(spec.items)} / {_en(spec.already)})",
            f"click {path}",
            f"verify in the database: {spec.signal}" if spec.signal else "no database signal",
        ],
        "allowed_by_env": actions_allowed(env),
    }
    if not confirm:
        result["hint"] = "Nothing was done. To run it: --confirm (MCP: confirm=true) with WA_ALLOW_ACTIONS=1."
        return result
    if not actions_allowed(env):
        raise ActionRefused("Action refused: set WA_ALLOW_ACTIONS=1 in the process environment.", plan=result)

    ui = ui or WhatsAppUI()
    result["mode"] = "executed"
    ui.ensure_unlocked()  # screen_locked before anything opens or gets clicked
    if not _open_chat(ui, rec["phone"]):
        result.update(reason="whatsapp_not_frontmost", note="WhatsApp did not become frontmost; nothing was clicked.")
        return result
    bar = _chat_menu(ui)
    item = ui.find_item(bar.path, spec.items)
    if item is None:
        current = ui.find_item(bar.path, spec.already)
        if current is not None:
            result.update(done=True, already=True, clicked=None, note=f"The menu already shows {current.title!r}: nothing to do.")
            after = wa.chat_state(jids, with_hidden=True)
            result["state_after"] = _shown(wa, jids, after)
            result["verified"] = bool(spec.check and after and spec.check(after))
            return result
        titles = [m.title for m in ui.items(bar.path)]
        raise ActionFailed(f"Item for {action!r} not found in the {_en(MENU_CHAT)!r} menu.", menu_items=titles)
    if not item.enabled:
        raise ActionFailed(f"The item {item.title!r} is disabled. Nothing was done.")
    target = item
    if spec.submenu:
        sub = ui.find_item(item.path, spec.submenu)
        if sub is None or not sub.enabled:
            raise ActionFailed(
                f"Option {_en(spec.submenu)!r} not found under {item.title!r}.",
                menu_items=[m.title for m in ui.items(item.path)],
            )
        target = sub
    if not ui.click(target):
        result.update(reason="whatsapp_not_frontmost", note="Another app came to the front before the click; nothing was clicked.")
        return result
    result.update(done=True, clicked=target.title, attempts=1)

    if spec.check is None:
        result.update(verified=False, reason="no_db_signal")
        return result
    # Verify against hidden chats too; only the answer leaves their state out.
    if wa.chat_state(jids, with_hidden=True) is None:
        result.update(verified=False, reason="chat_not_in_db")
        return result
    state: dict = {}

    def ok() -> bool:
        st = wa.chat_state(jids, with_hidden=True)
        state["after"] = st
        return bool(st and spec.check(st))

    verified = ui.wait_for(ok, CONFIRM_TIMEOUT)
    if not verified:
        # Sometimes the click does not take (seen when unarchiving). If the menu still
        # shows the previous state, try a second and last time.
        ui.sleep(RETRY_DELAY)
        retry = _retry_target(ui, spec)
        if retry is None:
            result["retry_skipped"] = "the menu already shows the new state; only the database did not change"
        elif retry == "not_frontmost":
            result["retry_skipped"] = "WhatsApp did not become frontmost for the second attempt"
        elif ui.click(retry):
            result["attempts"] = 2
            verified = ui.wait_for(ok, CONFIRM_TIMEOUT)
        else:
            result["retry_skipped"] = "another app came to the front before the second click"
    result["state_after"] = _shown(wa, jids, state.get("after"))
    result["verified"] = verified
    if not verified:
        result["reason"] = "not_seen_in_db"
        result["note"] = (
            f"Clicked {result['attempts']} time(s), but {spec.signal} did not change within "
            f"{int(CONFIRM_TIMEOUT)} s. Check WhatsApp before trying again."
        )
    return result


def _shown(wa: WhatsApp, jids: list[str], state: dict | None) -> dict | None:
    """The state to put in the answer: none for a chat the reads would hide."""
    return state if state is not None and wa.chat_state(jids) is not None else None


def _retry_target(ui: WhatsAppUI, spec: ActionSpec):
    """Item to click again if the menu still shows the previous state; None otherwise."""
    if not ui.bring_to_front():
        return "not_frontmost"
    bar = ui.find_bar(MENU_CHAT)
    if bar is None:
        return None
    item = ui.find_item(bar.path, spec.items)
    if item is None or not item.enabled:
        return None
    if spec.submenu:
        sub = ui.find_item(item.path, spec.submenu)
        return sub if sub is not None and sub.enabled else None
    return item


# ------------------------------------------------------------ calls


def start_call(
    wa: WhatsApp,
    to: str,
    kind: str = "voice",
    confirm: bool = False,
    env: Mapping[str, str] | None = None,
    ui: WhatsAppUI | None = None,
) -> dict:
    env = os.environ if env is None else env
    if kind not in CALL_ITEMS:
        raise InvalidArgument("kind must be 'voice' or 'video'")
    rec, _ = _recipient(wa, to)
    result: dict = {
        "kind": kind,
        "to": rec,
        "started": False,
        "mode": "plan",
        "plan": [
            f"open {chat_url(rec['phone'])}",
            "bring WhatsApp to the front",
            f"click {_en(MENU_CHAT)} > {_en(CALL_ITEMS[kind])}",
            f"verify that {_en(MENU_CALL)} > {_en(ITEM_HANGUP)} became enabled",
        ],
        "allowed_by_env": calls_allowed(env),
    }
    if not confirm:
        result["hint"] = "Nothing was done. To call: --confirm (MCP: confirm=true) with WA_ALLOW_CALLS=1."
        return result
    if not calls_allowed(env):
        raise ActionRefused("Call refused: set WA_ALLOW_CALLS=1 in the process environment.", plan=result)

    ui = ui or WhatsAppUI()
    result["mode"] = "executed"
    ui.ensure_unlocked()  # screen_locked before anything opens or gets clicked
    if call_active(ui):
        raise ActionFailed("A call is already in progress; end it first (wa hangup).")
    if not _open_chat(ui, rec["phone"]):
        result.update(reason="whatsapp_not_frontmost", note="WhatsApp did not become frontmost; nothing was clicked.")
        return result
    bar = _chat_menu(ui)
    item = ui.find_item(bar.path, CALL_ITEMS[kind])
    if item is None or not item.enabled:
        raise ActionFailed(f"Item {_en(CALL_ITEMS[kind])!r} not found or disabled. Nothing was done.")
    if not ui.click(item):
        result.update(reason="whatsapp_not_frontmost", note="Another app came to the front before the click; nothing was clicked.")
        return result
    result["started"] = True
    verified = ui.wait_for(lambda: call_active(ui), CALL_START_TIMEOUT)
    result["verified"] = verified
    if not verified:
        result["reason"] = "hangup_item_not_enabled"
        result["note"] = f"Clicked {item.title!r}, but {_en(ITEM_HANGUP)!r} did not become enabled."
    return result


def hangup(
    confirm: bool = False,
    env: Mapping[str, str] | None = None,
    ui: WhatsAppUI | None = None,
) -> dict:
    env = os.environ if env is None else env
    result: dict = {
        "done": False,
        "mode": "plan",
        "plan": [
            "bring WhatsApp to the front",
            f"click {_en(MENU_CALL)} > {_en(ITEM_HANGUP)}",
            "verify that the item became disabled",
        ],
        "allowed_by_env": calls_allowed(env),
    }
    if not confirm:
        result["hint"] = "Nothing was done. To hang up: --confirm (MCP: confirm=true) with WA_ALLOW_CALLS=1."
        return result
    if not calls_allowed(env):
        raise ActionRefused("Refused: set WA_ALLOW_CALLS=1 in the process environment.", plan=result)
    ui = ui or WhatsAppUI()
    result["mode"] = "executed"
    ui.ensure_unlocked()  # screen_locked before anything opens or gets clicked
    if not ui.bring_to_front():
        result.update(reason="whatsapp_not_frontmost")
        return result
    bar = ui.find_bar(MENU_CALL)
    item = ui.find_item(bar.path, ITEM_HANGUP) if bar else None
    if item is None or not item.enabled:
        result.update(reason="no_active_call", note="There is no call in progress.")
        return result
    if not ui.click(item):
        result.update(reason="whatsapp_not_frontmost")
        return result
    result["done"] = True
    result["verified"] = ui.wait_for(lambda: not call_active(ui), HANGUP_TIMEOUT)
    if not result["verified"]:
        result["reason"] = "call_still_active"
    return result


def select_call_device(ui: WhatsAppUI, submenu: tuple[str, ...] | str, fragment: str) -> str:
    """Pick a device in the Call > <submenu> menu (e.g. Microphone > BlackHole 16ch)."""
    titles = (submenu,) if isinstance(submenu, str) else submenu
    bar = ui.find_bar(MENU_CALL)
    if bar is None:
        raise ActionFailed(f"Menu {_en(MENU_CALL)!r} not found (did the call start?).")
    sub = ui.find_item(bar.path, titles)
    if sub is None:
        raise ActionFailed(f"Submenu {_en(titles)!r} not found in {_en(MENU_CALL)!r}.")
    dev = ui.find_item_containing(sub.path, fragment)
    if dev is None:
        raise ActionFailed(
            f"Device {fragment!r} does not appear in {_en(MENU_CALL)} > {_en(titles)}.",
            devices=[m.title for m in ui.items(sub.path)],
        )
    if not ui.click(dev):
        raise ActionFailed("WhatsApp left the front before the device could be picked.")
    return dev.title


__all__ = ["ACTIONS", "chat_action", "start_call", "hangup", "select_call_device", "call_active", "norm_title"]
