"""Smoke test against the REAL WhatsApp database on this Mac.

Prints ONLY counts and timings: never message text, names or phone numbers.

    uv run python scripts/smoke_real.py
"""

from __future__ import annotations

import sys
import time

from wa import db
from wa.core import WhatsApp


def timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    print(f"{label}: {time.perf_counter() - t0:.3f}s")
    return out


def main() -> int:
    try:
        conn = timed("open ChatStorage (ro)", lambda: db.connect(db.CHAT_DB))
    except db.WAError as exc:
        print(f"ERROR: {exc.code}")
        return 1
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print("chats (total):", q("SELECT count(*) FROM ZWACHATSESSION"))
    print("visible chats:", q("SELECT count(*) FROM ZWACHATSESSION WHERE COALESCE(ZREMOVED,0)=0 AND COALESCE(ZHIDDEN,0)=0"))
    print("hidden chats:", q("SELECT count(*) FROM ZWACHATSESSION WHERE ZHIDDEN=1"))
    print("messages (total):", q("SELECT count(*) FROM ZWAMESSAGE"))
    print("text messages:", q("SELECT count(*) FROM ZWAMESSAGE WHERE ZMESSAGETYPE=0"))
    conn.close()

    with WhatsApp() as wa:
        chats = timed("wa chats --limit 3", lambda: wa.list_chats(limit=3))
        print("  chats returned:", chats["count"])
        res = timed("wa search 'a' --limit 100", lambda: wa.search_messages("a", limit=100))
        print("  results:", res["count"], "| has next page:", bool(res["next_cursor"]))
        if chats["chats"]:
            msgs = timed("wa messages (1st chat) --limit 50", lambda: wa.read_messages(chats["chats"][0]["jid"], limit=50))
            print("  messages returned:", msgs["count"])
        cont = timed("contact index", lambda: (wa._load_contacts(), len(wa._contacts))[1])
        print("  address book contacts:", cont)
    return 0


if __name__ == "__main__":
    sys.exit(main())
