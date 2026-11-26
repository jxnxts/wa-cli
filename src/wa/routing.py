"""Picks the path for sending and actions: the companion (GoWA) or the Desktop app UI.

`via`:
- `auto` (default): the companion if it is connected, otherwise the app UI;
- `companion`: companion only (error if it is not connected);
- `desktop`: app UI only (the original behavior).

The guards do not change: without `confirm` you only get the plan/draft; with
`confirm`, sending needs WA_ALLOW_SEND=1 and actions need WA_ALLOW_ACTIONS=1,
whatever the path.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Mapping

from . import companion as comp
from .actions import ACTIONS, actions_allowed
from .actions import chat_action as desktop_chat_action
from .core import WhatsApp, phone_digits
from .db import APPLE_EPOCH_OFFSET, ActionRefused, InvalidArgument, SendRefused, WAError
from .send import MAX_SEND_CHARS, send_allowed, send_message, validate_text

ClientFactory = Callable[[], comp.GowaClient]


def _planned_via(via: str, client_factory: ClientFactory) -> tuple[str, str | None]:
    """Expected path for a draft/plan (without raising)."""
    if via not in comp.VIAS:
        raise InvalidArgument(f"invalid via: {via!r} (use {', '.join(comp.VIAS)})")
    if via == "desktop":
        return "desktop", None
    connected = comp.is_connected(client_factory())
    if connected:
        return "companion", None
    if via == "companion":
        return "companion", "the companion is not connected right now; sending will fail"
    return "desktop", "companion not connected"


def _desktop_check(wa: WhatsApp, target: dict, text: str, started: float, env: Mapping[str, str],
                   sleep: Callable[[float], None], clock: Callable[[], float]) -> dict | str:
    """Optional check in the Desktop database; never holds the answer for long."""
    try:
        wait = float(env.get("WA_COMPANION_DESKTOP_CHECK_S") or 0)
    except ValueError:
        wait = 0.0
    if wait <= 0 or target.get("kind") != "person":
        return "skipped"
    wait = min(wait, 10.0)
    try:
        if not target.get("phone"):
            return "skipped"
        jids = [target["jid"], *wa.jids_for_phone(phone_digits(target["phone"]))]
        since = started - APPLE_EPOCH_OFFSET - 2
        deadline = clock() + wait
        while True:
            found = wa.find_sent_message(list(dict.fromkeys(j for j in jids if j)), text, since)
            if found or clock() >= deadline:
                return found or "not_seen_yet"
            sleep(0.5)
    except WAError as exc:
        return {"error": exc.code}


def send(
    wa: WhatsApp,
    to: str,
    text: str,
    confirm: bool = False,
    via: str = "auto",
    method: str = "ui",
    reply_to: str | None = None,
    open_draft: bool = False,
    max_chars: int = MAX_SEND_CHARS,
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    desktop_send: Callable[..., dict] = send_message,
    **desktop_kwargs,
) -> dict:
    env = os.environ if env is None else env
    client_factory = client_factory or (lambda: comp.default_client(env))
    text = validate_text(text, max_chars)
    if method == "draft":
        via = "desktop"  # the draft in the app UI never sends
    if reply_to and via == "desktop":
        raise InvalidArgument("quoted replies (reply_to) only work through the companion (via companion or auto)")

    if not confirm:
        planned, why = _planned_via(via, client_factory)
        if planned == "desktop" and reply_to:
            raise InvalidArgument("quoted replies (reply_to) need the companion connected")
        if planned == "desktop":
            out = desktop_send(wa, to, text, confirm=False, method=method, open_draft=open_draft, env=env,
                               max_chars=max_chars, sleep=sleep, clock=clock, **desktop_kwargs)
        else:
            target = comp.resolve_target(wa, to)
            out = {"sent": False, "mode": "draft", "to": target, "text": text, "chars": len(text),
                   "reply_to": reply_to,
                   "hint": "Nothing was sent. To really send: confirm=true (CLI: --confirm) with WA_ALLOW_SEND=1.",
                   "send_allowed_by_env": send_allowed(env),
                   "rate": comp.check_rate(env, clock())}
        out["via"] = planned
        out["via_requested"] = via
        if why:
            out["via_note"] = why
        return out

    chosen, client, why = comp.choose_via(via, client_factory)
    if chosen == "companion":
        # Through the app UI, send_message itself checks the guard.
        if not send_allowed(env):
            raise SendRefused(
                "Send refused: set WA_ALLOW_SEND=1 in the process environment to allow sending.",
                draft={"to": to, "text": text, "via": "companion"},
            )
        started = clock()
        out = comp.send(client, wa, to, text, reply_to=reply_to, env=env, clock=clock)
        out["text"] = text
        out["chars"] = len(text)
        out["desktop_check"] = _desktop_check(wa, out["to"], text, started, env, sleep, clock)
    else:
        if reply_to:
            raise InvalidArgument("quoted replies (reply_to) need the companion connected; nothing was sent")
        out = desktop_send(wa, to, text, confirm=True, method=method, open_draft=False, env=env,
                           max_chars=max_chars, sleep=sleep, clock=clock, **desktop_kwargs)
        out["via"] = "desktop"
    out["via_requested"] = via
    if why:
        out["via_note"] = why
    return out


def chat_action(
    wa: WhatsApp,
    to: str,
    action: str,
    confirm: bool = False,
    via: str = "auto",
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory | None = None,
    ui=None,
    desktop_action: Callable[..., dict] = desktop_chat_action,
) -> dict:
    env = os.environ if env is None else env
    client_factory = client_factory or (lambda: comp.default_client(env))
    if action not in ACTIONS:
        raise InvalidArgument(f"invalid action: {action!r} (use {', '.join(ACTIONS)})")

    if not confirm:
        planned, why = _planned_via(via, client_factory)
        if planned == "desktop":
            out = desktop_action(wa, to, action, confirm=False, env=env, ui=ui)
        else:
            target = comp.resolve_target(wa, to)
            route, body = comp.COMPANION_ACTIONS[action]
            out = {"action": action, "to": target, "done": False, "mode": "plan",
                   "plan": [f"POST /chat/{target['jid']}/{route} {body} on GoWA (app state, syncs with the phone)"],
                   "allowed_by_env": actions_allowed(env),
                   "hint": "Nothing was done. To run it: --confirm (MCP: confirm=true) with WA_ALLOW_ACTIONS=1."}
            if target["kind"] == "person" and target.get("phone"):
                try:
                    out["state_before"] = wa.chat_state([target["jid"], *wa.jids_for_phone(phone_digits(target["phone"]))])
                except WAError:
                    pass
        out["via"] = planned
        out["via_requested"] = via
        if why:
            out["via_note"] = why
        return out

    chosen, client, why = comp.choose_via(via, client_factory)
    if chosen == "companion":
        # Through the app UI, the Desktop chat_action itself checks the guard.
        if not actions_allowed(env):
            raise ActionRefused("Action refused: set WA_ALLOW_ACTIONS=1 in the process environment.",
                                plan={"action": action, "to": to, "via": "companion"})
        out = comp.chat_action(client, wa, to, action)
    else:
        out = desktop_action(wa, to, action, confirm=True, env=env, ui=ui)
        out["via"] = "desktop"
    out["via_requested"] = via
    if why:
        out["via_note"] = why
    return out
