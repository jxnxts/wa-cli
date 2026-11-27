"""`wa` CLI: reads WhatsApp Desktop on this Mac and, with confirmation, sends messages.

Output is JSON (use --pretty to indent). Errors are JSON too, with a non-zero
exit code.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import WhatsApp
from .db import InvalidArgument, WAError
from .actions import ACTIONS, chat_action, hangup, start_call
from . import companion as comp
from . import routing
from .send import DEFAULT_METHOD, METHODS, send_message
from .ui import WhatsAppUI
from . import media as media_mod
from . import media_send

CLI_MAX_RESPONSE_CHARS = 2_000_000


def _ui_factory() -> WhatsAppUI:  # swapped in tests
    return WhatsAppUI()


def _companion_factory() -> comp.GowaClient:  # swapped in tests
    return comp.default_client()


def _service():  # swapped in tests
    from . import service

    return service


def _voice_deps():  # swapped in tests
    from .voice import VoiceDeps

    return VoiceDeps(ui=_ui_factory())


def _common_out(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pretty", action="store_true", help="indented JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wa",
        description="Reads WhatsApp Desktop on this Mac (read-only) and sends messages with confirmation.",
    )
    parser.add_argument("--version", action="version", version=f"wa {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("chats", help="list chats")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--unread", action="store_true", help="only chats with unread messages")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--groups", action="store_const", dest="kind", const="groups", help="groups only")
    g.add_argument("--direct", action="store_const", dest="kind", const="direct", help="one-to-one chats only")
    g.add_argument("--kind", choices=["direct", "group", "broadcast", "status", "community", "channel"])
    p.add_argument("--query", "-q", help="filter by name (case and accent insensitive)")
    p.add_argument("--archived", action="store_true", default=None, help="archived chats only")
    p.add_argument("--include-hidden", action="store_true", help="include hidden chats (ZHIDDEN=1)")
    _common_out(p)

    p = sub.add_parser("messages", help="messages of a chat (newest first)")
    p.add_argument("--chat", required=True, help="jid, phone number or name")
    p.add_argument("--since", help="ISO (2026-11-01) or relative (7d, 12h)")
    p.add_argument("--until", help="ISO or relative")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor", help="next_cursor from the previous page")
    p.add_argument("--include-system", action="store_true", help="include group system notices")
    p.add_argument("--media", choices=media_mod.FILTERS, help="only messages with this kind of media (any for all)")
    p.add_argument("--include-hidden", action="store_true")
    _common_out(p)

    p = sub.add_parser("search", help="search text (case and accent insensitive)")
    p.add_argument("text")
    p.add_argument("--chat", help="limit to one chat (jid, phone number or name)")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor")
    p.add_argument("--include-hidden", action="store_true")
    _common_out(p)

    p = sub.add_parser("contacts", help="resolve a name or phone number -> phone/jid")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-hidden", action="store_true")
    _common_out(p)

    p = sub.add_parser("send", help="draft a message; really sends only with --confirm and WA_ALLOW_SEND=1")
    p.add_argument("--to", required=True,
                   help="phone number with country code, jid (@s.whatsapp.net/@lid), contact name or group jid (…@g.us); "
                        "broadcast lists (@broadcast) and channels (@newsletter) are refused")
    p.add_argument("--text", help="the message (or use --file)")
    p.add_argument("--file", help="send a file through the companion; outside the outbox folder only with --allow-any-path")
    p.add_argument("--caption", help="caption for an image, video or document (with --file)")
    p.add_argument("--as", dest="as_kind", choices=media_send.SEND_KINDS,
                   help="how to send the file (default: from its type; voice converts to ogg/opus with ffmpeg)")
    p.add_argument("--allow-any-path", action="store_true",
                   help="accept a --file outside the outbox folder (WA_MEDIA_OUTBOX, default ~/.local/share/wa-mac/outbox)")
    p.add_argument("--confirm", action="store_true", help="really send (needs WA_ALLOW_SEND=1)")
    p.add_argument("--via", choices=comp.VIAS, default="auto",
                   help="auto (default): the companion if it is connected, otherwise the app UI; companion; desktop")
    p.add_argument("--reply-to", help="id of the message to quote (companion only)")
    p.add_argument("--method", choices=METHODS, default=None,
                   help="through the app UI (desktop): ui (default) opens the chat (a group through whatsapp://openchat/<jid>, "
                        "with the text pasted), presses Return with WhatsApp in front and checks the database; needs an "
                        "unlocked screen; shortcut: the 'WA Send' shortcut (people only); draft: only opens the chat")
    _common_out(p)

    p = sub.add_parser("media", help="read the media of a message: image, document text, OCR, transcript, video frames")
    p.add_argument("--message", required=True, help="message id (the 'id' field of wa messages / wa search)")
    p.add_argument("--no-ocr", action="store_true", help="skip OCR of images and scanned PDFs")
    p.add_argument("--no-transcribe", action="store_true", help="skip the transcript of audio and video")
    p.add_argument("--frames", type=int, help="video frames to extract (default 4, maximum 8, 0 for none)")
    p.add_argument("--max-pages", type=int, help="pages, sheets or slides to read (default 10, maximum 50)")
    p.add_argument("--max-chars", type=int, help="characters of text to return (default 20000)")
    p.add_argument("--page-image", action="store_true", default=None,
                   help="always add the first page of a PDF as an image (default: only when it has little text)")
    p.add_argument("--no-download", action="store_true", help="do not ask the companion for files missing on the Mac")
    p.add_argument("--include-hidden", action="store_true")
    _common_out(p)

    p = sub.add_parser("media-cache", help="size of the OCR/transcript cache; --clear deletes it")
    p.add_argument("--clear", action="store_true")
    _common_out(p)

    p = sub.add_parser("chat-action", help="archive, pin, mute and so on (a plan; runs with --confirm and WA_ALLOW_ACTIONS=1)")
    p.add_argument("--to", required=True, help="phone number with country code, jid or name; a group by jid (…@g.us) only through the companion")
    p.add_argument("--action", required=True, choices=list(ACTIONS))
    p.add_argument("--via", choices=comp.VIAS, default="auto",
                   help="auto (default): the companion if it is connected, otherwise the menu bar; companion; desktop")
    p.add_argument("--confirm", action="store_true")
    _common_out(p)

    p = sub.add_parser("companion", help="companion mode (GoWA): pairing, status, inbox, service")
    csub = p.add_subparsers(dest="ccmd", required=True)
    q = csub.add_parser("link", help="pair this Mac with the 8-character code")
    q.add_argument("--phone", help="number with country code (default: your own, from WhatsApp Desktop)")
    q.add_argument("--timeout", type=float, default=comp.LINK_TIMEOUT, help="seconds to wait for the code to be used")
    _common_out(q)
    q = csub.add_parser("status", help="is the companion connected?")
    _common_out(q)
    q = csub.add_parser("logout", help="unlink this Mac from the account (--confirm)")
    q.add_argument("--confirm", action="store_true")
    _common_out(q)
    q = csub.add_parser("inbox", help="received messages the companion captured (does not mark them as read)")
    q.add_argument("--since", default="24h", help="ISO or relative (default 24h)")
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--unread", action="store_true", help="unread only")
    _common_out(q)
    q = csub.add_parser("service", help="GoWA LaunchAgent")
    q.add_argument("op", choices=["start", "stop", "status"])
    _common_out(q)

    p = sub.add_parser("list-create", help="create a chat list (companion; --confirm and WA_ALLOW_ACTIONS=1)")
    p.add_argument("--name", required=True)
    p.add_argument("--confirm", action="store_true")
    _common_out(p)
    for name, helptext in (("list-add", "add a chat to a list"), ("list-remove", "remove a chat from a list")):
        p = sub.add_parser(name, help=f"{helptext} (companion; --confirm and WA_ALLOW_ACTIONS=1)")
        p.add_argument("--list", required=True, dest="list_ref", help="list name or id")
        p.add_argument("--chat", required=True, help="phone number, jid or name; a group by jid")
        p.add_argument("--confirm", action="store_true")
        _common_out(p)
    p = sub.add_parser("list-delete", help="delete a list (companion; --confirm and WA_ALLOW_ACTIONS=1)")
    p.add_argument("--list", required=True, dest="list_ref")
    p.add_argument("--confirm", action="store_true")
    _common_out(p)

    p = sub.add_parser("call", help="call through WhatsApp (a plan; calls with --confirm and WA_ALLOW_CALLS=1)")
    p.add_argument("--to", required=True)
    p.add_argument("--kind", choices=["voice", "video"], default="voice")
    p.add_argument("--confirm", action="store_true")
    _common_out(p)

    p = sub.add_parser("hangup", help="end the call in progress (--confirm and WA_ALLOW_CALLS=1)")
    p.add_argument("--confirm", action="store_true")
    _common_out(p)

    p = sub.add_parser("calls", help="call history")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--missed", action="store_true", help="missed only")
    _common_out(p)

    p = sub.add_parser("lists", help="chat lists (from the companion when it is connected; WhatsApp Desktop does not show them)")
    _common_out(p)

    p = sub.add_parser("voice-call", help="call and let the voice agent talk (--confirm and WA_ALLOW_CALLS=1)")
    p.add_argument("--to", required=True)
    p.add_argument("--goal", required=True, help="goal of the call")
    p.add_argument("--max-minutes", type=float, default=5)
    p.add_argument("--monitor", action="store_true", help="mirror the audio to the default speaker")
    p.add_argument("--confirm", action="store_true")
    _common_out(p)

    p = sub.add_parser("voice-test", help="talk to the agent through the Mac's microphone/speaker, without WhatsApp")
    p.add_argument("--goal", required=True)
    p.add_argument("--max-minutes", type=float, default=3)
    _common_out(p)

    p = sub.add_parser("mcp", help="start the MCP server (stdio)")
    return parser


class _LazyUI:
    """Creates the UI object only if the path is the Desktop app."""

    def __init__(self):
        self._ui = None

    def __getattr__(self, name):
        if self._ui is None:
            self._ui = _ui_factory()
        return getattr(self._ui, name)


def _print_code(info: dict) -> None:
    sys.stderr.write(
        "\n"
        f"  Pairing code: {info['code']}\n"
        f"  Number: {info['phone']}\n"
        f"  {info['instructions']}\n"
        f"  Waiting up to {info['expires_in_s']} s (checking every 2 s)...\n\n"
    )
    sys.stderr.flush()


def _run_companion(args) -> dict:
    if args.ccmd == "service":
        svc = _service()
        return {"start": svc.start, "stop": svc.stop, "status": svc.status}[args.op]()
    client = _companion_factory()
    if args.ccmd == "status":
        return comp.status(client)
    if args.ccmd == "link":
        return comp.link(client, phone=args.phone, on_code=_print_code, timeout=args.timeout)
    if args.ccmd == "logout":
        return comp.logout(client, confirm=args.confirm)
    if args.ccmd == "inbox":
        return comp.inbox(client, since=args.since, limit=args.limit, unread_only=args.unread)
    raise SystemExit(2)  # pragma: no cover


def _emit(data, pretty: bool) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2 if pretty else None)
    sys.stdout.write("\n")


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    pretty = getattr(args, "pretty", False)
    if args.cmd == "companion":
        try:
            data = _run_companion(args)
        except WAError as exc:
            _emit(exc.to_dict(), pretty)
            return 1
        _emit(data, pretty)
        if args.ccmd == "link" and not data.get("linked"):
            return 1
        if args.ccmd == "logout" and args.confirm and not data.get("done"):
            return 1
        if args.ccmd == "service" and args.op == "start" and not data.get("started"):
            return 1
        return 0
    if args.cmd == "media-cache":
        from . import cache

        _emit(cache.clear() if args.clear else cache.stats(), pretty)
        return 0
    if args.cmd == "voice-test":
        from .voice import voice_test

        try:
            data = voice_test(args.goal, args.max_minutes, deps=_voice_deps())
        except WAError as exc:
            _emit(exc.to_dict(), pretty)
            return 1
        _emit(data, pretty)
        return 0
    try:
        with WhatsApp(include_hidden=getattr(args, "include_hidden", False)) as wa:
            if args.cmd == "chats":
                data = wa.list_chats(limit=args.limit, unread=args.unread, kind=args.kind, query=args.query, archived=args.archived)
            elif args.cmd == "messages":
                data = wa.read_messages(
                    args.chat, since=args.since, until=args.until, limit=args.limit,
                    cursor=args.cursor, include_system=args.include_system,
                    max_chars=CLI_MAX_RESPONSE_CHARS, media_kind=args.media,
                )
            elif args.cmd == "search":
                data = wa.search_messages(
                    args.text, chat=args.chat, since=args.since, until=args.until,
                    limit=args.limit, cursor=args.cursor, max_chars=CLI_MAX_RESPONSE_CHARS,
                )
            elif args.cmd == "contacts":
                data = wa.find_contacts(args.query, limit=args.limit)
            elif args.cmd == "media":
                data, _images = media_mod.get_media(
                    wa, args.message, ocr=not args.no_ocr, transcribe=not args.no_transcribe, frames=args.frames,
                    max_pages=args.max_pages, max_chars=args.max_chars, page_image=args.page_image,
                    download=not args.no_download, save_images=True, client_factory=_companion_factory,
                )
            elif args.cmd == "send" and args.file:
                if args.text:
                    raise InvalidArgument("with --file, put the text in --caption")
                if args.method is not None:
                    # A file always goes through the companion; --method (draft above all) would
                    # be silently ignored and the file sent.
                    raise InvalidArgument("--method does not apply to --file: files go through the companion. "
                                          "Leave out --confirm to see the plan without sending.")
                data = media_send.send_media(
                    wa, args.to, args.file, caption=args.caption, as_kind=args.as_kind, confirm=args.confirm,
                    via=args.via, reply_to=args.reply_to, allow_any_path=args.allow_any_path,
                    client_factory=_companion_factory,
                )
            elif args.cmd == "send":
                if args.caption or args.as_kind or args.allow_any_path:
                    raise InvalidArgument("--caption, --as and --allow-any-path go with --file")
                if args.text is None:
                    raise InvalidArgument("give --text or --file")
                data = routing.send(
                    wa, args.to, args.text, confirm=args.confirm, via=args.via, method=args.method or DEFAULT_METHOD,
                    reply_to=args.reply_to, open_draft=(args.method == "draft"),
                    client_factory=_companion_factory, desktop_send=send_message,
                )
            elif args.cmd == "chat-action":
                data = routing.chat_action(wa, args.to, args.action, confirm=args.confirm, via=args.via,
                                           client_factory=_companion_factory, ui=_LazyUI(),
                                           desktop_action=chat_action)
            elif args.cmd in ("list-create", "list-add", "list-remove", "list-delete"):
                op = args.cmd.split("-", 1)[1]
                data = comp.list_op(op, wa, confirm=args.confirm, client=_companion_factory(),
                                    name=getattr(args, "name", None), list_ref=getattr(args, "list_ref", None),
                                    chat=getattr(args, "chat", None))
            elif args.cmd == "call":
                data = start_call(wa, args.to, kind=args.kind, confirm=args.confirm, ui=_ui_factory())
            elif args.cmd == "hangup":
                data = hangup(confirm=args.confirm, ui=_ui_factory())
            elif args.cmd == "voice-call":
                from .voice import voice_call

                data = voice_call(wa, args.to, args.goal, args.max_minutes, monitor=args.monitor,
                                  confirm=args.confirm, deps=_voice_deps())
            elif args.cmd == "calls":
                data = wa.list_calls(limit=args.limit, missed=args.missed)
            elif args.cmd == "lists":
                data = comp.lists(wa, _companion_factory())
            else:  # pragma: no cover
                raise SystemExit(2)
    except WAError as exc:
        _emit(exc.to_dict(), pretty)
        return 1
    _emit(data, pretty)
    if args.cmd == "send" and args.confirm and args.method != "draft" and not data.get("sent"):
        return 1
    if args.cmd in ("chat-action", "hangup", "list-create", "list-add", "list-remove", "list-delete") and args.confirm and not data.get("done"):
        return 1
    if args.cmd == "voice-call" and args.confirm and (not data.get("called") or data.get("error")):
        return 1
    if args.cmd == "call" and args.confirm and not data.get("started"):
        return 1
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
