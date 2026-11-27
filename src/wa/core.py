"""WhatsApp read core (shared by the CLI and the MCP server)."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, media
from .db import (
    APPLE_EPOCH_OFFSET,
    AmbiguousError,
    InvalidArgument,
    NotFoundError,
    WAError,
    normalize,
)

SESSION_KINDS = {
    0: "direct",
    1: "group",
    2: "broadcast",
    3: "status",
    4: "community",
    5: "channel",
}
KIND_CODES = {v: k for k, v in SESSION_KINDS.items()}
# Status (stories) is not a chat: left out by default.
DEFAULT_KINDS = (0, 1, 2, 4, 5)
GROUP_KINDS = (1, 4)

MESSAGE_TYPES = {
    0: "text",
    1: "image",
    2: "video",
    3: "audio",
    4: "contact",
    5: "location",
    6: "system",
    7: "link",
    8: "document",
    11: "gif",
    14: "deleted",
    15: "sticker",
}
SYSTEM_TYPE = 6

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_TEXT_CHARS = 4000  # per message
DEFAULT_MAX_RESPONSE_CHARS = 60_000

ME = "Me"


# ---------------------------------------------------------------- helpers


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    if limit is None:
        return default
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise InvalidArgument("limit must be an integer") from None
    return max(1, min(limit, maximum))


def apple_to_iso(value) -> str | None:
    if value is None:
        return None
    try:
        ts = float(value) + APPLE_EPOCH_OFFSET
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


_REL = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)


def parse_when(value: str | None) -> float | None:
    """Convert ISO (`2026-11-01`, `2026-11-01T10:00`) or relative (`7d`, `12h`,
    `30m`, `2w`) into seconds since 2001-01-01 (the Core Data format)."""
    if value is None or value == "":
        return None
    m = _REL.match(str(value))
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        dt = datetime.now(timezone.utc) - delta
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise InvalidArgument(
                f"invalid date: {value!r}. Use ISO (2026-11-01 or 2026-11-01T10:00) or relative (7d, 12h, 30m)."
            ) from None
        if dt.tzinfo is None:
            dt = dt.astimezone()  # local time
    return dt.timestamp() - APPLE_EPOCH_OFFSET


def encode_cursor(date, pk) -> str:
    raw = json.dumps([date if date is not None else 0, int(pk)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None):
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        date, pk = json.loads(base64.urlsafe_b64decode(cursor + pad))
        return float(date), int(pk)
    except Exception:
        raise InvalidArgument("invalid cursor") from None


def phone_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def looks_like_phone(value: str) -> bool:
    return bool(re.fullmatch(r"[\s+()\-.\d]+", value or "")) and len(phone_digits(value)) >= 8


def jid_user(jid: str | None) -> str | None:
    if not jid or "@" not in jid:
        return None
    return jid.split("@", 1)[0].split(":", 1)[0]


PERSON_JID = re.compile(r"\d+@(s\.whatsapp\.net|lid)")
GROUP_JID = re.compile(r"\d+(-\d+)?@g\.us")


def _e164_digits(phone: str | None, wa_jid: str | None) -> str:
    """Digits with the country code of an address book contact, or "" when unsure.

    ZPHONENUMBER is what the user typed and is often local ("(61) 98888-7777"), so only
    a number written with "+" counts; otherwise the WhatsApp id does."""
    if phone and phone.strip().startswith("+"):
        return phone_digits(phone)
    if wa_jid and PERSON_JID.fullmatch(wa_jid) and wa_jid.endswith("@s.whatsapp.net"):
        return jid_user(wa_jid)
    return ""


def br_variants(digits: str) -> list[str]:
    """Brazilian mobile numbers show up both with and without the ninth digit."""
    out = [digits]
    if digits.startswith("55"):
        if len(digits) == 13 and digits[4] == "9":
            out.append(digits[:4] + digits[5:])
        elif len(digits) == 12 and digits[4] not in "2345":
            # Only mobile numbers got the ninth digit; landlines start with 2-5 and never do.
            out.append(digits[:4] + "9" + digits[4:])
    return out


def _like_pattern(text: str) -> str:
    esc = normalize(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "ignore") or None
        except Exception:
            return None
    return str(value)


# ---------------------------------------------------------------- reader


class WhatsApp:
    """Read-only reader for the WhatsApp databases."""

    def __init__(self, base: Path | None = None, include_hidden: bool = False):
        self.base = base
        self.include_hidden = include_hidden
        self._conn: sqlite3.Connection | None = None
        self._contacts: list[dict] | None = None
        self._name_by_jid: dict[str, str] | None = None
        self._phone_by_lid: dict[str, str] | None = None
        self._lids_by_phone: dict[str, set[str]] | None = None
        self._push_by_jid: dict[str, str] | None = None
        self._media_cols: set[str] | None = None

    # ------------------------------------------------------------ connections

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = db.connect(db.CHAT_DB, self.base)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _optional_db(self, name: str) -> sqlite3.Connection | None:
        try:
            return db.connect(name, self.base)
        except db.NotInstalledError:
            return None

    # ------------------------------------------------------------ indexes

    def _load_contacts(self) -> None:
        if self._contacts is not None:
            return
        contacts: list[dict] = []
        name_by_jid: dict[str, str] = {}
        phone_by_lid: dict[str, str] = {}
        lids_by_phone: dict[str, set[str]] = {}

        conn = self._optional_db(db.CONTACTS_DB)
        if conn is not None:
            try:
                cols = {r["name"] for r in db.query(conn, "SELECT name FROM pragma_table_info('ZWAADDRESSBOOKCONTACT')")}
                def col(c):
                    return c if c in cols else f"NULL AS {c}"
                rows = db.query(
                    conn,
                    f"SELECT {col('ZFULLNAME')}, {col('ZGIVENNAME')}, {col('ZBUSINESSNAME')}, "
                    f"{col('ZPHONENUMBER')}, {col('ZWHATSAPPID')}, {col('ZLID')}, {col('ZUSERNAME')} "
                    "FROM ZWAADDRESSBOOKCONTACT",
                )
            finally:
                conn.close()
            for r in rows:
                name = r["ZFULLNAME"] or r["ZGIVENNAME"] or r["ZBUSINESSNAME"]
                wa_jid = r["ZWHATSAPPID"]
                lid = r["ZLID"]
                trusted = _e164_digits(r["ZPHONENUMBER"], wa_jid)
                digits = trusted or phone_digits(r["ZPHONENUMBER"])
                contacts.append(
                    {
                        "name": name,
                        "given_name": r["ZGIVENNAME"],
                        "business_name": r["ZBUSINESSNAME"],
                        "username": r["ZUSERNAME"],
                        "phone": f"+{digits}" if digits else None,
                        "jid": wa_jid or (f"{digits}@s.whatsapp.net" if digits else None),
                        "lid": lid,
                        "wa_id": wa_jid,
                    }
                )
                for j in (wa_jid, lid):
                    if j and name:
                        name_by_jid.setdefault(j, name)
                if lid and trusted:
                    phone_by_lid.setdefault(lid, digits)
                    lids_by_phone.setdefault(digits, set()).add(lid)

        conn = self._optional_db(db.LID_DB)
        if conn is not None:
            try:
                rows = db.query(
                    conn,
                    "SELECT ZIDENTIFIER, ZPHONENUMBER FROM ZWAZACCOUNT "
                    "WHERE ZIDENTIFIER IS NOT NULL AND ZPHONENUMBER IS NOT NULL",
                )
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()
            for r in rows:
                lid = r["ZIDENTIFIER"]
                if "@" not in lid:
                    lid = f"{lid}@lid"
                digits = phone_digits(r["ZPHONENUMBER"])
                if digits:
                    phone_by_lid.setdefault(lid, digits)
                    lids_by_phone.setdefault(digits, set()).add(lid)

        self._contacts = contacts
        self._name_by_jid = name_by_jid
        self._phone_by_lid = phone_by_lid
        self._lids_by_phone = lids_by_phone

    def _load_pushnames(self) -> None:
        if self._push_by_jid is not None:
            return
        try:
            rows = db.query(self.conn, "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID IS NOT NULL")
            self._push_by_jid = {r["ZJID"]: _as_text(r["ZPUSHNAME"]) for r in rows if _as_text(r["ZPUSHNAME"])}
        except sqlite3.OperationalError:
            self._push_by_jid = {}

    def contact_name(self, jid: str | None) -> str | None:
        if not jid:
            return None
        self._load_contacts()
        return self._name_by_jid.get(jid)

    def phone_for_jid(self, jid: str | None) -> str | None:
        """Phone number (digits only, with country code) of a person JID, if known."""
        if not jid:
            return None
        if jid.endswith("@s.whatsapp.net"):
            return jid_user(jid)
        if jid.endswith("@lid"):
            self._load_contacts()
            return self._phone_by_lid.get(jid)
        return None

    def jids_for_phone(self, digits: str) -> list[str]:
        self._load_contacts()
        out: list[str] = []
        for d in br_variants(digits):
            out.append(f"{d}@s.whatsapp.net")
            out.extend(sorted(self._lids_by_phone.get(d, ())))
        return list(dict.fromkeys(out))

    def send_jids_for_phone(self, digits: str) -> list[str]:
        """JIDs to address a person by, best first, for clients (GoWA) that need the jid WhatsApp
        knows: the jid of an existing one-to-one chat (often an @lid), then the contact's own
        WhatsApp id (ZWHATSAPPID, which can be the old Brazilian number without the ninth digit),
        then the number itself with and without the ninth digit."""
        digits = phone_digits(digits)
        if not digits:
            return []
        self._load_contacts()
        known = self.jids_for_phone(digits)
        where, params = self._chat_filter("s", (0,))
        rows = db.query(
            self.conn,
            f"SELECT s.ZCONTACTJID AS jid FROM ZWACHATSESSION s WHERE {where} "
            f"AND s.ZCONTACTJID IN ({','.join('?' * len(known))}) "
            "ORDER BY (s.ZCONTACTJID LIKE '%@lid') DESC, s.ZLASTMESSAGEDATE DESC",
            [*params, *known],
        )
        out = [r["jid"] for r in rows if r["jid"]]
        variants = br_variants(digits)
        for c in self._contacts:
            if c.get("wa_id") and c["phone"] and phone_digits(c["phone"]) in variants:
                out.append(c["wa_id"])
        out += [f"{d}@s.whatsapp.net" for d in variants]
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------ filters

    def _chat_filter(self, alias: str = "s", kinds=DEFAULT_KINDS) -> tuple[str, list]:
        clauses = [f"COALESCE({alias}.ZREMOVED, 0) = 0"]
        params: list = []
        if not self.include_hidden:
            clauses.append(f"COALESCE({alias}.ZHIDDEN, 0) = 0")
        if kinds:
            clauses.append(f"{alias}.ZSESSIONTYPE IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        return " AND ".join(clauses), params

    def _chat_dict(self, r: sqlite3.Row) -> dict:
        kind = SESSION_KINDS.get(r["ZSESSIONTYPE"], f"type_{r['ZSESSIONTYPE']}")
        jid = r["ZCONTACTJID"]
        out = {
            "jid": jid,
            "name": r["ZPARTNERNAME"] or self.contact_name(jid),
            "kind": kind,
            "unread": int(r["ZUNREADCOUNT"] or 0),
            "archived": bool(r["ZARCHIVED"]),
            "last_message_at": apple_to_iso(r["lastd"]),
        }
        if r["pinned"]:
            out["pinned"] = True
        if kind == "direct":
            digits = self.phone_for_jid(jid)
            out["phone"] = f"+{digits}" if digits else None
        if self.include_hidden:
            out["hidden"] = bool(r["ZHIDDEN"])
        return out

    # Pinned chats store ZLASTMESSAGEDATE in the far future (used for sorting).
    # In that case the real date comes from the last message (ZLASTMESSAGE).
    _FUTURE = "(CAST(strftime('%s','now') AS REAL) - 978307200 + 86400 * 30)"
    _CHAT_COLS = (
        "s.Z_PK, s.ZCONTACTJID, s.ZPARTNERNAME, s.ZSESSIONTYPE, s.ZUNREADCOUNT, "
        "s.ZARCHIVED, s.ZHIDDEN, "
        f"(s.ZLASTMESSAGEDATE > {_FUTURE}) AS pinned, "
        f"CASE WHEN s.ZLASTMESSAGEDATE > {_FUTURE} "
        "THEN (SELECT lm.ZMESSAGEDATE FROM ZWAMESSAGE lm WHERE lm.Z_PK = s.ZLASTMESSAGE) "
        "ELSE s.ZLASTMESSAGEDATE END AS lastd"
    )

    # ------------------------------------------------------------ chats

    def list_chats(
        self,
        limit: int | None = None,
        unread: bool = False,
        kind: str | None = None,
        query: str | None = None,
        archived: bool | None = None,
    ) -> dict:
        limit = clamp_limit(limit)
        if kind in (None, "", "all"):
            kinds = DEFAULT_KINDS
        elif kind in ("groups", "group"):
            kinds = (1,)
        elif kind == "direct":
            kinds = (0,)
        elif kind in KIND_CODES:
            kinds = (KIND_CODES[kind],)
        else:
            raise InvalidArgument(f"unknown chat kind: {kind!r}")
        where, params = self._chat_filter("s", kinds)
        if unread:
            where += " AND COALESCE(s.ZUNREADCOUNT, 0) != 0"
        if archived is not None:
            where += " AND COALESCE(s.ZARCHIVED, 0) = ?"
            params.append(1 if archived else 0)
        if query:
            where += " AND (wa_norm(s.ZPARTNERNAME) LIKE ? ESCAPE '\\' OR s.ZCONTACTJID LIKE ? ESCAPE '\\')"
            pat = _like_pattern(query)
            params.extend([pat, pat])
        rows = db.query(
            self.conn,
            f"SELECT {self._CHAT_COLS} FROM ZWACHATSESSION s WHERE {where} "
            "ORDER BY lastd IS NULL, lastd DESC, s.Z_PK DESC LIMIT ?",
            [*params, limit],
        )
        chats = [self._chat_dict(r) for r in rows]
        return {"count": len(chats), "chats": chats}

    def resolve_chat(self, ref: str) -> sqlite3.Row:
        """Find a chat by JID, phone number or name."""
        if not ref or not str(ref).strip():
            raise InvalidArgument("give the chat (jid, phone number or name)")
        ref = str(ref).strip()
        where, params = self._chat_filter("s", None)
        base_sql = f"SELECT {self._CHAT_COLS} FROM ZWACHATSESSION s WHERE {where}"

        def by_jids(jids: list[str]):
            if not jids:
                return []
            return db.query(
                self.conn,
                f"{base_sql} AND s.ZCONTACTJID IN ({','.join('?' * len(jids))}) "
                "ORDER BY lastd DESC",
                [*params, *jids],
            )

        if "@" in ref:
            jids = [ref]
            digits = self.phone_for_jid(ref)
            if digits:
                jids += self.jids_for_phone(digits)
            rows = by_jids(list(dict.fromkeys(jids)))
            if rows:
                return rows[0]
            raise NotFoundError(f"chat not found: {ref}")

        if looks_like_phone(ref):
            rows = by_jids(self.jids_for_phone(phone_digits(ref)))
            if rows:
                return rows[0]
            raise NotFoundError(f"no chat with phone number {ref}")

        norm = normalize(ref)
        rows = db.query(self.conn, f"{base_sql} AND wa_norm(s.ZPARTNERNAME) = ? ORDER BY lastd DESC", [*params, norm])
        if not rows:
            rows = db.query(
                self.conn,
                f"{base_sql} AND wa_norm(s.ZPARTNERNAME) LIKE ? ESCAPE '\\' ORDER BY lastd DESC",
                [*params, _like_pattern(ref)],
            )
        if not rows:
            # Address book name that does not show up as ZPARTNERNAME.
            jids: list[str] = []
            for c in self._match_contacts(ref):
                jids += [j for j in (c["jid"], c["lid"]) if j]
            rows = by_jids(list(dict.fromkeys(jids)))
        if not rows:
            raise NotFoundError(f"chat not found: {ref}")
        unique = {r["Z_PK"]: r for r in rows}
        if len(unique) > 1:
            raise AmbiguousError(
                f"more than one chat matches {ref!r}; use the jid",
                candidates=[{"jid": r["ZCONTACTJID"], "name": r["ZPARTNERNAME"], "kind": SESSION_KINDS.get(r["ZSESSIONTYPE"])} for r in list(unique.values())[:10]],
            )
        return rows[0]

    # ------------------------------------------------------------ messages

    # ZWAMEDIAITEM column -> alias. Columns missing from this WhatsApp build read as NULL.
    _MEDIA_COLS = {
        "ZVCARDSTRING": "mi_mime",
        "ZFILESIZE": "mi_size",
        "ZMOVIEDURATION": "mi_dur",
        "ZTITLE": "mi_title",
        "ZAUTHORNAME": "mi_fname",
        "ZMEDIALOCALPATH": "mi_path",
        "ZMEDIAORIGIN": "mi_origin",
    }

    def _media_columns(self) -> set[str]:
        if self._media_cols is None:
            try:
                rows = db.query(self.conn, "SELECT name FROM pragma_table_info('ZWAMEDIAITEM')")
                self._media_cols = {r["name"] for r in rows}
            except sqlite3.OperationalError:
                self._media_cols = set()
        return self._media_cols

    @property
    def _MSG_SQL(self) -> str:  # noqa: N802 (kept from when it was a constant)
        cols = self._media_columns()
        joinable = bool(cols)
        media_sel = ", ".join(
            (f"mi.{c} AS {a}" if (joinable and c in cols) else f"NULL AS {a}") for c, a in self._MEDIA_COLS.items()
        )
        join = "LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM " if joinable else ""
        return (
            "SELECT m.Z_PK AS pk, COALESCE(m.ZMESSAGEDATE, 0) AS d, m.ZTEXT AS text, "
            "m.ZISFROMME AS me, m.ZMESSAGETYPE AS mtype, m.ZFROMJID AS fromjid, "
            "m.ZPUSHNAME AS push, m.ZSTANZAID AS stanza, gm.ZMEMBERJID AS mjid, "
            "gm.ZCONTACTNAME AS mname, s.ZCONTACTJID AS chatjid, s.ZPARTNERNAME AS chatname, "
            f"s.ZSESSIONTYPE AS stype, {media_sel} "
            "FROM ZWAMESSAGE m "
            "JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION "
            "LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER "
            f"{join}"
        )

    def media_base(self) -> Path:
        """Folder that ZMEDIALOCALPATH is relative to (`<data dir>/Message`)."""
        return (self.base or db.data_dir()) / "Message"

    def message_row(self, message_id) -> sqlite3.Row:
        """One message by id (Z_PK), with its chat and media columns. Hidden chats follow include_hidden."""
        try:
            pk = int(str(message_id).strip())
        except (TypeError, ValueError):
            raise InvalidArgument("message id must be the number in the 'id' field of wa messages / wa search") from None
        where, params = self._chat_filter("s", None)
        rows = db.query(self.conn, f"{self._MSG_SQL} WHERE {where} AND m.Z_PK = ?", [*params, pk])
        if not rows:
            raise NotFoundError(f"message not found: {pk}")
        return rows[0]

    def message_summary(self, r: sqlite3.Row) -> dict:
        return self._msg_dict(r, with_chat=True)

    def _author(self, r: sqlite3.Row) -> dict:
        if r["me"]:
            return {"name": ME, "jid": None}
        stype = r["stype"]
        if stype in GROUP_KINDS:
            jid = r["mjid"]
            if not jid and r["fromjid"] and not str(r["fromjid"]).endswith("@g.us"):
                jid = r["fromjid"]
            name = self.contact_name(jid) or r["mname"]
            if not name:
                self._load_pushnames()
                name = self._push_by_jid.get(jid) if jid else None
            name = name or _as_text(r["push"])
            return {"name": name, "jid": jid}
        if stype == 0:
            jid = r["chatjid"]
            return {"name": self.contact_name(jid) or r["chatname"], "jid": jid}
        jid = r["fromjid"]
        return {"name": self.contact_name(jid) or _as_text(r["push"]), "jid": jid}

    def _msg_dict(self, r: sqlite3.Row, with_chat: bool) -> dict:
        text = r["text"]
        out: dict = {
            "id": r["pk"],
            "date": apple_to_iso(r["d"]),
            "from_me": bool(r["me"]),
            "from": self._author(r),
            "type": MESSAGE_TYPES.get(r["mtype"], f"type_{r['mtype']}"),
            "text": text,
        }
        if text is not None and len(text) > MAX_TEXT_CHARS:
            out["text"] = text[:MAX_TEXT_CHARS]
            out["text_truncated"] = True
        info = media.describe(r, self.media_base())
        if info:
            out["media"] = info
        if with_chat:
            out["chat"] = {
                "jid": r["chatjid"],
                "name": r["chatname"],
                "kind": SESSION_KINDS.get(r["stype"], f"type_{r['stype']}"),
            }
        return out

    def _page(self, where: str, params: list, limit, cursor, max_chars, with_chat) -> dict:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        if cur:
            where += " AND (COALESCE(m.ZMESSAGEDATE, 0) < ? OR (COALESCE(m.ZMESSAGEDATE, 0) = ? AND m.Z_PK < ?))"
            params = [*params, cur[0], cur[0], cur[1]]
        rows = db.query(
            self.conn,
            f"{self._MSG_SQL} WHERE {where} ORDER BY COALESCE(m.ZMESSAGEDATE, 0) DESC, m.Z_PK DESC LIMIT ?",
            [*params, limit + 1],
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        budget = max_chars or DEFAULT_MAX_RESPONSE_CHARS
        used = 0
        items: list[dict] = []
        last = None
        truncated = False
        for r in rows:
            item = self._msg_dict(r, with_chat)
            size = len(json.dumps(item, ensure_ascii=False))
            if items and used + size > budget:
                truncated = True
                break
            items.append(item)
            used += size
            last = r
        more = has_more or truncated
        return {
            "count": len(items),
            "order": "newest_first",
            "messages": items,
            "next_cursor": encode_cursor(last["d"], last["pk"]) if (more and last is not None) else None,
            "truncated_by_size": truncated,
        }

    def _date_filters(self, since, until) -> tuple[str, list]:
        sql, params = "", []
        s, u = parse_when(since), parse_when(until)
        if s is not None:
            sql += " AND COALESCE(m.ZMESSAGEDATE, 0) >= ?"
            params.append(s)
        if u is not None:
            sql += " AND COALESCE(m.ZMESSAGEDATE, 0) <= ?"
            params.append(u)
        return sql, params

    def read_messages(
        self,
        chat: str,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        include_system: bool = False,
        max_chars: int | None = None,
        media_kind: str | None = None,
    ) -> dict:
        row = self.resolve_chat(chat)
        where = "m.ZCHATSESSION = ?"
        params: list = [row["Z_PK"]]
        if not include_system:
            where += f" AND COALESCE(m.ZMESSAGETYPE, 0) != {SYSTEM_TYPE}"
        if media_kind:
            where += " AND " + media.filter_sql(media_kind, self._media_columns())
        dsql, dparams = self._date_filters(since, until)
        page = self._page(where + dsql, params + dparams, limit, cursor, max_chars, with_chat=False)
        return {"chat": self._chat_dict(row), **page}

    def search_messages(
        self,
        text: str,
        chat: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        max_chars: int | None = None,
    ) -> dict:
        if not text or not normalize(text).strip():
            raise InvalidArgument("give the text to search for")
        cwhere, params = self._chat_filter("s")
        pat = _like_pattern(text)
        # Text, plus media captions and document file names when this build has those columns.
        fields = ["m.ZTEXT"] + [f"mi.{c}" for c in ("ZTITLE", "ZAUTHORNAME") if c in self._media_columns()]
        where = f"{cwhere} AND (" + " OR ".join(f"wa_norm({f}) LIKE ? ESCAPE '\\'" for f in fields) + ")"
        params.extend([pat] * len(fields))
        chat_info = None
        if chat:
            row = self.resolve_chat(chat)
            where += " AND m.ZCHATSESSION = ?"
            params.append(row["Z_PK"])
            chat_info = self._chat_dict(row)
        dsql, dparams = self._date_filters(since, until)
        page = self._page(where + dsql, params + dparams, limit, cursor, max_chars, with_chat=True)
        out = {"query": text, **page}
        if chat_info:
            out["chat"] = chat_info
        return out

    # ------------------------------------------------------------ contacts

    def _match_contacts(self, ref: str) -> list[dict]:
        self._load_contacts()
        if looks_like_phone(ref):
            digits = phone_digits(ref)
            variants = br_variants(digits)
            return [
                c for c in self._contacts
                if c["phone"] and any(v in phone_digits(c["phone"]) for v in variants)
            ]
        norm = normalize(ref).strip()
        # A full name, business name or username that matches exactly wins on its own. A first
        # name does not: "Ana" must stay ambiguous between "Ana Lúcia" and "Ana Clara".
        exact, partial = [], []
        for c in self._contacts:
            strong = [normalize(c.get(k)) for k in ("name", "business_name", "username")]
            fields = strong + [normalize(c.get("given_name"))]
            if norm in strong:
                exact.append(c)
            elif any(norm in f for f in fields if f):
                partial.append(c)
        return exact or partial

    def find_contacts(self, ref: str, limit: int | None = None) -> dict:
        if not ref or not str(ref).strip():
            raise InvalidArgument("give a name or phone number")
        limit = clamp_limit(limit)
        results: list[dict] = []
        seen: set[str] = set()

        # Existing chats (by JID), to fill in chat_jid.
        where, params = self._chat_filter("s", (0,))
        chat_rows = db.query(self.conn, f"SELECT s.ZCONTACTJID, s.ZPARTNERNAME FROM ZWACHATSESSION s WHERE {where}", params)
        chat_by_jid = {r["ZCONTACTJID"]: r["ZPARTNERNAME"] for r in chat_rows if r["ZCONTACTJID"]}

        def add(entry: dict):
            key = entry.get("jid") or entry.get("lid") or entry.get("chat_jid")
            if not key or key in seen:
                return
            for k in ("jid", "lid", "chat_jid"):
                if entry.get(k):
                    seen.add(entry[k])
            results.append(entry)

        for c in self._match_contacts(ref):
            chat_jid = next((j for j in (c["jid"], c["lid"]) if j and j in chat_by_jid), None)
            add({"name": c["name"], "phone": c["phone"], "jid": c["jid"], "lid": c["lid"], "chat_jid": chat_jid, "source": "contacts"})

        # One-to-one chats whose name matches (contacts outside the address book).
        if looks_like_phone(ref):
            jids = self.jids_for_phone(phone_digits(ref))
            matches = [(j, chat_by_jid[j]) for j in jids if j in chat_by_jid]
        else:
            norm = normalize(ref).strip()
            matches = [(j, n) for j, n in chat_by_jid.items() if n and norm in normalize(n)]
        for jid, name in matches:
            digits = self.phone_for_jid(jid)
            add(
                {
                    "name": self.contact_name(jid) or name,
                    "phone": f"+{digits}" if digits else None,
                    "jid": f"{digits}@s.whatsapp.net" if digits else (jid if jid.endswith("@s.whatsapp.net") else None),
                    "lid": jid if jid.endswith("@lid") else None,
                    "chat_jid": jid,
                    "source": "chats",
                }
            )
        return {"query": ref, "count": min(len(results), limit), "contacts": results[:limit]}

    def find_sent_message(self, jids: list[str], text: str, since_apple: float) -> dict | None:
        """Look for a message sent by me with ZTEXT == text after `since_apple`.

        First in the chats of `jids`; if not found (a new chat with an unknown
        JID), in any chat, flagged with `chat_matched=False`.
        """
        base = (
            "SELECT m.Z_PK AS pk, m.ZMESSAGESTATUS AS status, m.ZMESSAGEDATE AS d, "
            "s.ZCONTACTJID AS chatjid FROM ZWAMESSAGE m "
            "JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION "
            "WHERE m.ZISFROMME = 1 AND m.ZTEXT = ? AND m.ZMESSAGEDATE >= ?"
        )
        params: list = [text, since_apple]
        rows = []
        if jids:
            rows = db.query(
                self.conn,
                f"{base} AND s.ZCONTACTJID IN ({','.join('?' * len(jids))}) ORDER BY m.Z_PK DESC LIMIT 1",
                [*params, *jids],
            )
        matched = bool(rows)
        if not rows:
            # The fallback looks in any chat, so it must not surface hidden ones.
            where, fparams = self._chat_filter("s", kinds=None)
            rows = db.query(self.conn, f"{base} AND {where} ORDER BY m.Z_PK DESC LIMIT 1", [*params, *fparams])
        if not rows:
            return None
        r = rows[0]
        return {
            "message_id": r["pk"],
            "status": r["status"],
            "date": apple_to_iso(r["d"]),
            "chat_jid": r["chatjid"],
            "chat_matched": matched,
        }

    # ------------------------------------------------------------ chat state

    def chat_state(self, jids: list[str]) -> dict | None:
        """State (read-only) of the one-to-one chat with one of these JIDs. Hidden chats follow
        include_hidden, so a plan never reveals the state of a chat the reads would not show."""
        jids = [j for j in dict.fromkeys(jids) if j]
        if not jids:
            return None
        where, params = self._chat_filter("s", kinds=None)
        rows = db.query(
            self.conn,
            f"SELECT s.Z_PK, s.ZCONTACTJID, s.ZARCHIVED, s.ZUNREADCOUNT, "
            f"(s.ZLASTMESSAGEDATE > {self._FUTURE}) AS pinned, s.ZPROPERTIES "
            f"FROM ZWACHATSESSION s WHERE {where} "
            f"AND s.ZCONTACTJID IN ({','.join('?' * len(jids))}) ORDER BY s.ZLASTMESSAGEDATE DESC",
            [*params, *jids],
        )
        if not rows:
            return None
        r = rows[0]
        mute_until = None
        try:
            prop = db.query(
                self.conn,
                "SELECT ZMUTEDATE FROM ZWACHATPROPERTIES WHERE ZCHATSESSION = ? OR Z_PK = ? "
                "ORDER BY ZMUTEDATE IS NULL, ZMUTEDATE DESC LIMIT 1",
                [r["Z_PK"], r["ZPROPERTIES"]],
            )
            if prop and prop[0]["ZMUTEDATE"] is not None:
                mute_until = float(prop[0]["ZMUTEDATE"])
        except sqlite3.OperationalError:
            prop = None
        now_apple = time.time() - APPLE_EPOCH_OFFSET
        return {
            "chat_jid": r["ZCONTACTJID"],
            "archived": bool(r["ZARCHIVED"]),
            "pinned": bool(r["pinned"]),
            "unread": int(r["ZUNREADCOUNT"] or 0),
            "muted": mute_until is not None and mute_until > now_apple,
            "mute_until": apple_to_iso(mute_until) if mute_until and mute_until < now_apple + 86400 * 365 * 50 else ("always" if mute_until else None),
        }

    # ------------------------------------------------------------ calls

    def list_calls(self, limit: int | None = None, missed: bool = False) -> dict:
        limit = clamp_limit(limit)
        conn = self._optional_db(db.CALLS_DB)
        if conn is None:
            return {"count": 0, "calls": []}
        try:
            where = "1=1"
            if missed:
                where = "COALESCE(a.ZMISSED, 0) = 1"
            try:
                rows = db.query(
                    conn,
                    "SELECT e.Z_PK AS pk, e.ZDATE AS d, e.ZDURATION AS dur, e.ZOUTCOME AS outcome, "
                    "e.ZGROUPJIDSTRING AS gjid, a.ZINCOMING AS incoming, a.ZMISSED AS missed, "
                    "a.ZVIDEO AS video FROM ZWACDCALLEVENT e "
                    "LEFT JOIN ZWAAGGREGATECALLEVENT a ON a.Z_PK = e.Z1CALLEVENTS "
                    f"WHERE {where} ORDER BY e.ZDATE DESC, e.Z_PK DESC LIMIT ?",
                    [limit],
                )
            except sqlite3.OperationalError:
                return {"count": 0, "calls": []}
            parts: dict[int, list[str]] = {}
            if rows:
                pks = [r["pk"] for r in rows]
                for p in db.query(
                    conn,
                    f"SELECT Z1PARTICIPANTS AS ev, ZJIDSTRING AS jid FROM ZWACDCALLEVENTPARTICIPANT "
                    f"WHERE Z1PARTICIPANTS IN ({','.join('?' * len(pks))})",
                    pks,
                ):
                    if p["jid"]:
                        parts.setdefault(p["ev"], []).append(p["jid"])
        finally:
            conn.close()
        calls = []
        for r in rows:
            people = []
            for jid in parts.get(r["pk"], []):
                digits = self.phone_for_jid(jid)
                people.append({"jid": jid, "name": self.contact_name(jid), "phone": f"+{digits}" if digits else None})
            calls.append(
                {
                    "id": r["pk"],
                    "date": apple_to_iso(r["d"]),
                    "duration_s": round(float(r["dur"] or 0), 1),
                    "direction": "incoming" if r["incoming"] else "outgoing",
                    "missed": bool(r["missed"]),
                    "video": bool(r["video"]),
                    "outcome": r["outcome"],
                    "group_jid": r["gjid"],
                    "participants": people,
                }
            )
        return {"count": len(calls), "calls": calls}

    # ------------------------------------------------------------ lists

    def list_lists(self) -> dict:
        lists: dict = {}
        conn = self._optional_db(db.LABELS_DB)
        if conn is not None:
            try:
                for r in db.query(conn, "SELECT Z_PK, ZLABELID, ZNAME, ZTYPE, ZISACTIVE FROM ZWASMBLABEL ORDER BY ZORDERINDEX, Z_PK"):
                    key = r["ZLABELID"] if r["ZLABELID"] is not None else f"pk{r['Z_PK']}"
                    lists[key] = {"id": key, "name": r["ZNAME"], "type": r["ZTYPE"], "active": bool(r["ZISACTIVE"]), "members": [], "_pk": r["Z_PK"]}
                by_pk = {v["_pk"]: v for v in lists.values()}
                for r in db.query(
                    conn,
                    "SELECT j.Z_1LABELS AS label, p.ZCONTACTJID AS jid FROM Z_1PROXIES j "
                    "JOIN ZWASMBLABELEDENTITYPROXY p ON p.Z_PK = j.Z_2PROXIES",
                ):
                    if r["label"] in by_pk and r["jid"]:
                        by_pk[r["label"]]["members"].append(r["jid"])
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        conn = self._optional_db(db.BACKED_UP_DB)
        if conn is not None:
            try:
                for r in db.query(conn, "SELECT label_id, chat_jid FROM list_member"):
                    entry = lists.setdefault(r["label_id"], {"id": r["label_id"], "name": None, "type": None, "active": None, "members": []})
                    if r["chat_jid"] not in entry["members"]:
                        entry["members"].append(r["chat_jid"])
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        out = []
        for v in lists.values():
            v.pop("_pk", None)
            v["members"] = [{"jid": j, "name": self.contact_name(j)} for j in v["members"]]
            out.append(v)
        return {"count": len(out), "lists": out}

    def resolve_group(self, jid: str) -> dict:
        """A known group (a chat in the database) -> {name, jid, phone: None, kind: "group"}."""
        where, params = self._chat_filter("s", None)
        rows = db.query(
            self.conn,
            f"SELECT s.ZCONTACTJID, s.ZPARTNERNAME, s.ZSESSIONTYPE FROM ZWACHATSESSION s "
            f"WHERE {where} AND s.ZCONTACTJID = ? ORDER BY s.ZLASTMESSAGEDATE DESC LIMIT 1",
            [*params, jid],
        )
        if not rows:
            raise NotFoundError(f"group not found among the known chats: {jid}")
        r = rows[0]
        kind = SESSION_KINDS.get(r["ZSESSIONTYPE"])
        if kind == "community":
            raise InvalidArgument(
                f"{jid} is a community, not a group: you cannot send to the community itself. "
                "Use the jid of its announcements group or of one of its groups (wa chats --groups)."
            )
        if kind != "group":
            raise InvalidArgument(f"{jid} is not a group (kind {kind or r['ZSESSIONTYPE']})")
        return {"name": r["ZPARTNERNAME"], "jid": jid, "phone": None, "kind": "group"}

    def resolve_recipient(self, to: str) -> dict:
        """Resolve a send recipient -> {name, jid, phone, kind}.

        - a person (phone number, @s.whatsapp.net, @lid or address book name): `kind="person"`,
          `phone` in +E164 and `jid` in the @s.whatsapp.net form;
        - a group (`…@g.us`, must be among the known chats): `kind="group"`, `phone=None`
          and `jid` the @g.us itself.
        Broadcast lists (@broadcast), status and channels (@newsletter) are refused.
        """
        if not to or not str(to).strip():
            raise InvalidArgument("give the recipient")
        to = str(to).strip()
        if "@" in to:
            if to.endswith("@g.us"):
                if not GROUP_JID.fullmatch(to):
                    raise InvalidArgument(f"invalid group jid: {to}")
                return self.resolve_group(to)
            if to.endswith("@broadcast"):
                raise InvalidArgument(
                    "sending to broadcast lists and status (@broadcast) is not supported; "
                    "use a person (phone number, @s.whatsapp.net, @lid) or a group (@g.us)"
                )
            if to.endswith("@newsletter"):
                raise InvalidArgument(
                    "sending to channels (@newsletter) is not supported; "
                    "use a person (phone number, @s.whatsapp.net, @lid) or a group (@g.us)"
                )
            if not PERSON_JID.fullmatch(to):
                raise InvalidArgument(
                    "invalid recipient: use a phone number, @s.whatsapp.net, @lid (person) or @g.us (group), "
                    "with digits before the @"
                )
            digits = self.phone_for_jid(to)
            if not digits:
                raise NotFoundError(f"could not find the phone number of contact {to}")
            return {
                "name": self.contact_name(to) or self.contact_name(f"{digits}@s.whatsapp.net"),
                "jid": f"{digits}@s.whatsapp.net",
                "phone": f"+{digits}",
                "kind": "person",
            }
        if looks_like_phone(to):
            digits = phone_digits(to)
            if len(digits) < 10:
                raise InvalidArgument("the phone number needs the country and area code (e.g. +1 415 555 0123)")
            jid = f"{digits}@s.whatsapp.net"
            return {"name": self.contact_name(jid), "jid": jid, "phone": f"+{digits}", "kind": "person"}
        found = [c for c in self.find_contacts(to, limit=MAX_LIMIT)["contacts"] if c.get("phone")]
        if not found:
            groups = self.list_chats(kind="group", query=to, limit=10)["chats"]
            if groups:
                raise NotFoundError(
                    f"no contact called {to!r}; to send to a group, use its jid (…@g.us)",
                    candidates=[{"name": g["name"], "jid": g["jid"], "kind": "group"} for g in groups],
                )
            raise NotFoundError(f"contact not found or without a phone number: {to}")
        by_phone = {c["phone"]: c for c in found}
        if len(by_phone) > 1:
            exact = [c for c in by_phone.values() if normalize(c["name"]) == normalize(to)]
            if len(exact) == 1:
                by_phone = {exact[0]["phone"]: exact[0]}
            else:
                raise AmbiguousError(
                    f"more than one contact matches {to!r}; use the phone number",
                    candidates=[{"name": c["name"], "phone": c["phone"]} for c in list(by_phone.values())[:10]],
                )
        c = next(iter(by_phone.values()))
        return {
            "name": c["name"],
            "jid": c["jid"] or f"{phone_digits(c['phone'])}@s.whatsapp.net",
            "phone": c["phone"],
            "kind": "person",
        }


__all__ = ["WhatsApp", "WAError", "SESSION_KINDS", "MESSAGE_TYPES", "parse_when"]
