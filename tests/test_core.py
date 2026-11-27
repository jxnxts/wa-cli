import os
import sqlite3
from datetime import datetime

import pytest

from conftest import ANA_JID, BRUNO_LID, BRUNO_PHONE, CARLA_JID, DANI_JID, EDU_LID, GROUP_JID
from wa import db
from wa.core import WhatsApp, decode_cursor, encode_cursor, parse_when
from wa.db import AmbiguousError, FullDiskAccessError, InvalidArgument, NotFoundError, NotInstalledError


# ------------------------------------------------------------ connection


def test_connection_is_read_only(data_dir):
    conn = db.connect(db.CHAT_DB, data_dir)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM ZWAMESSAGE")
    conn.close()


def test_connection_reads_wal(tmp_path):
    from conftest import build_dbs

    base = build_dbs(tmp_path / "w")
    writer = sqlite3.connect(base / "ChatStorage.sqlite")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE ZWACHATSESSION SET ZPARTNERNAME='Novo nome' WHERE Z_PK=1")
    writer.commit()  # stays in the WAL while the writer is open
    conn = db.connect(db.CHAT_DB, base)
    assert conn.execute("SELECT ZPARTNERNAME FROM ZWACHATSESSION WHERE Z_PK=1").fetchone()[0] == "Novo nome"
    conn.close()
    writer.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_permission_denied_reports_full_disk_access(data_dir):
    path = data_dir / db.CHAT_DB
    path.chmod(0)
    try:
        with pytest.raises(FullDiskAccessError) as exc:
            db.connect(db.CHAT_DB, data_dir)
        assert "Full Disk Access" in exc.value.message
        assert exc.value.to_dict()["error"] == "full_disk_access_required"
    finally:
        path.chmod(0o644)


def test_missing_database(tmp_path):
    with pytest.raises(NotInstalledError):
        db.connect(db.CHAT_DB, tmp_path)


def test_busy_database_retries(data_dir, monkeypatch):
    calls = {"n": 0}
    real = sqlite3.connect

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(db, "_RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(db.sqlite3, "connect", flaky)
    conn = db.connect(db.CHAT_DB, data_dir)
    assert calls["n"] == 3
    conn.close()


def test_normalize():
    assert db.normalize("Ação CAFÉ") == "acao cafe"
    assert db.normalize(None) == ""


# ------------------------------------------------------------ helpers


def test_parse_when_iso_and_relative():
    assert parse_when(None) is None
    iso = parse_when("2026-11-01T10:00:00+00:00")
    assert iso == datetime.fromisoformat("2026-11-01T10:00:00+00:00").timestamp() - 978307200
    assert parse_when("7d") < parse_when("1h")
    with pytest.raises(InvalidArgument):
        parse_when("ontem")


def test_cursor_roundtrip():
    assert decode_cursor(encode_cursor(123.456, 42)) == (123.456, 42)
    with pytest.raises(InvalidArgument):
        decode_cursor("lixo!")


# ------------------------------------------------------------ chats


def test_list_chats_default_excludes_hidden_removed_status(wa):
    chats = wa.list_chats()["chats"]
    jids = [c["jid"] for c in chats]
    assert ANA_JID in jids and GROUP_JID in jids and BRUNO_LID in jids
    assert "5561900000001@s.whatsapp.net" not in jids  # hidden
    assert "5561900000002@s.whatsapp.net" not in jids  # removed
    assert "status@broadcast" not in jids
    # sorted by the real date (the pinned chat uses the date of its last message)
    assert jids[0] == ANA_JID
    carla = next(c for c in chats if c["jid"] == CARLA_JID)
    assert carla["pinned"] is True
    assert carla["last_message_at"].startswith(str(datetime.now().year)[:2])
    assert jids.index(CARLA_JID) > jids.index(BRUNO_LID)


def test_list_chats_fields(wa):
    ana = wa.list_chats(query="ana lucia")["chats"]
    assert len(ana) == 1
    c = ana[0]
    assert c["kind"] == "direct" and c["unread"] == 2 and c["phone"] == "+5561911112222"
    datetime.fromisoformat(c["last_message_at"])
    assert "hidden" not in c


def test_list_chats_filters(wa):
    assert [c["kind"] for c in wa.list_chats(kind="groups")["chats"]] == ["group"]
    assert {c["kind"] for c in wa.list_chats(kind="direct")["chats"]} == {"direct"}
    unread = wa.list_chats(unread=True)["chats"]
    assert {c["jid"] for c in unread} == {ANA_JID, GROUP_JID}
    assert wa.list_chats(limit=2)["count"] == 2
    assert wa.list_chats(limit=1000)["count"] <= 100
    assert wa.list_chats(query="FAMILIA")["chats"][0]["jid"] == GROUP_JID
    with pytest.raises(InvalidArgument):
        wa.list_chats(kind="xyz")


def test_include_hidden(data_dir):
    with WhatsApp(include_hidden=True) as w:
        chats = w.list_chats()["chats"]
    hidden = [c for c in chats if c["hidden"]]
    assert [c["name"] for c in hidden] == ["Secret"]


def test_lid_chat_has_phone(wa):
    bruno = wa.list_chats(query="bruno")["chats"][0]
    assert bruno["jid"] == BRUNO_LID and bruno["phone"] == f"+{BRUNO_PHONE}"


# ------------------------------------------------------------ resolving a chat


def test_resolve_chat_variants(wa):
    assert wa.resolve_chat(ANA_JID)["Z_PK"] == 1
    assert wa.resolve_chat("+55 (61) 91111-2222")["Z_PK"] == 1
    assert wa.resolve_chat("556111112222")["Z_PK"] == 1  # without the ninth digit
    assert wa.resolve_chat(f"+{BRUNO_PHONE}")["Z_PK"] == 2  # phone -> LID
    assert wa.resolve_chat(f"{BRUNO_PHONE}@s.whatsapp.net")["Z_PK"] == 2
    assert wa.resolve_chat("familia")["Z_PK"] == 3
    assert wa.resolve_chat("Ana Lúcia")["Z_PK"] == 1  # an exact name beats a partial one
    assert wa.resolve_chat("Bruno Lima")["Z_PK"] == 2  # address book name


def test_resolve_chat_errors(wa):
    with pytest.raises(AmbiguousError) as exc:
        wa.resolve_chat("ana")
    assert len(exc.value.details["candidates"]) == 2
    with pytest.raises(NotFoundError):
        wa.resolve_chat("nobody")
    with pytest.raises(NotFoundError):
        wa.resolve_chat("Secret")  # hidden
    with pytest.raises(InvalidArgument):
        wa.resolve_chat(" ")


# ------------------------------------------------------------ messages


def test_read_messages_direct(wa):
    r = wa.read_messages(ANA_JID)
    assert r["chat"]["jid"] == ANA_JID and r["order"] == "newest_first"
    ids = [m["id"] for m in r["messages"]]
    assert ids == [104, 103, 102, 101, 100]
    mine = next(m for m in r["messages"] if m["id"] == 101)
    assert mine["from_me"] is True and mine["from"]["name"] == "Me"
    theirs = next(m for m in r["messages"] if m["id"] == 100)
    assert theirs["from"] == {"name": "Ana Lúcia Souza", "jid": ANA_JID}
    assert next(m for m in r["messages"] if m["id"] == 103)["type"] == "image"
    datetime.fromisoformat(theirs["date"])
    assert r["next_cursor"] is None


def test_read_messages_group_authors(wa):
    r = wa.read_messages("Família")
    by_id = {m["id"]: m for m in r["messages"]}
    assert 301 not in by_id  # system events are left out by default
    assert by_id[300]["from"] == {"name": "Daniela Rocha", "jid": DANI_JID}  # address book
    assert by_id[302]["from"] == {"name": "Edu Push", "jid": EDU_LID}  # profile push name
    assert by_id[303]["from_me"] is True
    r2 = wa.read_messages("Família", include_system=True)
    assert 301 in {m["id"] for m in r2["messages"]}


def test_read_messages_pagination_with_ties(wa):
    seen = []
    cursor = None
    for _ in range(10):
        page = wa.read_messages(ANA_JID, limit=2, cursor=cursor)
        seen += [m["id"] for m in page["messages"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [104, 103, 102, 101, 100]


def test_read_messages_date_filters(wa):
    r = wa.read_messages(ANA_JID, since="1d")
    assert [m["id"] for m in r["messages"]] == [104, 103, 102]
    r = wa.read_messages(ANA_JID, until="1d")
    assert [m["id"] for m in r["messages"]] == [101, 100]


def test_response_size_budget(wa):
    r = wa.read_messages(ANA_JID, max_chars=10)
    assert r["count"] == 1 and r["truncated_by_size"] is True and r["next_cursor"]
    nxt = wa.read_messages(ANA_JID, cursor=r["next_cursor"], max_chars=10)
    assert nxt["messages"][0]["id"] == 103


# ------------------------------------------------------------ search


def test_search_case_and_accent_insensitive(wa):
    r = wa.search_messages("cafe")
    ids = [m["id"] for m in r["messages"]]
    assert ids == [302, 102]  # hidden, removed and status are left out
    assert r["messages"][0]["chat"] == {"jid": GROUP_JID, "name": "Família", "kind": "group"}
    assert [m["id"] for m in wa.search_messages("CAFÉ DA MANHA")["messages"]] == [302]


def test_search_filters(wa):
    assert [m["id"] for m in wa.search_messages("café", chat=ANA_JID)["messages"]] == [102]
    assert wa.search_messages("café", since="3h")["count"] == 1
    assert [m["id"] for m in wa.search_messages("100%")["messages"]] == [104]
    assert wa.search_messages("100_")["count"] == 0  # wildcards are escaped
    p1 = wa.search_messages("hi", limit=1)
    assert p1["count"] == 1 and p1["next_cursor"]
    p2 = wa.search_messages("hi", limit=1, cursor=p1["next_cursor"])
    assert p2["messages"][0]["id"] != p1["messages"][0]["id"]
    with pytest.raises(InvalidArgument):
        wa.search_messages("  ")


def test_search_include_hidden(data_dir):
    with WhatsApp(include_hidden=True) as w:
        ids = [m["id"] for m in w.search_messages("café")["messages"]]
    assert 400 in ids and 500 not in ids and 600 not in ids


# ------------------------------------------------------------ contacts


def test_find_contacts_by_name_and_phone(wa):
    r = wa.find_contacts("daniela")
    assert r["contacts"][0]["phone"] == "+5561955556666"
    assert r["contacts"][0]["chat_jid"] is None
    r = wa.find_contacts("bruno")
    c = r["contacts"][0]
    assert c["jid"] == f"{BRUNO_PHONE}@s.whatsapp.net" and c["lid"] == BRUNO_LID and c["chat_jid"] == BRUNO_LID
    r = wa.find_contacts("61 91111-2222")
    assert r["contacts"][0]["name"] == "Ana Lúcia Souza" and r["contacts"][0]["chat_jid"] == ANA_JID
    r = wa.find_contacts("carla")  # not in the address book, only a chat
    assert r["contacts"][0]["source"] == "chats" and r["contacts"][0]["phone"] == "+5521977778888"


def test_resolve_recipient(wa):
    assert wa.resolve_recipient("bruno")["phone"] == f"+{BRUNO_PHONE}"
    assert wa.resolve_recipient(BRUNO_LID)["phone"] == f"+{BRUNO_PHONE}"
    assert wa.resolve_recipient("+55 61 91111-2222")["jid"] == ANA_JID
    assert wa.resolve_recipient("Ana Paula")["phone"] == "+5561922223333"
    assert wa.resolve_recipient("bruno")["kind"] == "person"
    with pytest.raises(AmbiguousError):
        wa.resolve_recipient("ana")
    with pytest.raises(InvalidArgument):
        wa.resolve_recipient("123456789")
    with pytest.raises(NotFoundError):
        wa.resolve_recipient("12345@lid")


def test_resolve_recipient_group(wa, monkeypatch):
    monkeypatch.setattr(wa, "jids_for_phone", lambda *_: pytest.fail("a group has no phone number"))
    assert wa.resolve_recipient(GROUP_JID) == {"name": "Família", "jid": GROUP_JID, "phone": None, "kind": "group"}
    with pytest.raises(NotFoundError):
        wa.resolve_recipient("120363999999999999@g.us")  # unknown group


def test_resolve_recipient_group_name_hints_jid(wa):
    # A group name does not resolve on its own (so nothing goes to the wrong place), but it suggests the jid.
    with pytest.raises(NotFoundError) as exc:
        wa.resolve_recipient("Família")
    assert exc.value.details["candidates"] == [{"name": "Família", "jid": GROUP_JID, "kind": "group"}]


@pytest.mark.parametrize(
    "to,word",
    [
        ("status@broadcast", "broadcast lists"),
        ("123456789@broadcast", "broadcast lists"),
        ("120363000000000099@newsletter", "channels"),
        ("abc@c.us", "invalid recipient"),
    ],
)
def test_resolve_recipient_refuses_lists_and_channels(wa, to, word):
    with pytest.raises(InvalidArgument) as exc:
        wa.resolve_recipient(to)
    assert word in exc.value.message


# ------------------------------------------------------------ recipients that went to the wrong person


def _add_contacts(data_dir, rows, chats=()):
    c = sqlite3.connect(data_dir / "ContactsV2.sqlite")
    for pk, full, given, phone, wa_id, lid in rows:
        c.execute(
            "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZFULLNAME, ZGIVENNAME, ZPHONENUMBER, ZWHATSAPPID, ZLID) "
            "VALUES (?,?,?,?,?,?)", (pk, full, given, phone, wa_id, lid))
    c.commit()
    c.close()
    c = sqlite3.connect(data_dir / "ChatStorage.sqlite")
    for pk, jid, name in chats:
        c.execute("INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZLASTMESSAGEDATE) "
                  "VALUES (?, ?, ?, 0, 700000000)", (pk, jid, name))
    c.commit()
    c.close()


def test_br_variants_never_add_a_ninth_digit_to_a_landline():
    from wa.core import br_variants

    assert br_variants("551133334444") == ["551133334444"]  # São Paulo landline
    assert br_variants("556181199067") == ["556181199067", "5561981199067"]  # old mobile
    assert br_variants("5561981199067") == ["5561981199067", "556181199067"]


def test_first_name_alone_does_not_hide_other_matches(data_dir):
    _add_contacts(data_dir, [
        (60, "Xavi Maria", "Xavi", "+5561944445555", "5561944445555@s.whatsapp.net", None),
        (61, "Xavier Lopes", "Xavier", "+5561944446666", "5561944446666@s.whatsapp.net", None),
    ])
    with WhatsApp() as w:
        with pytest.raises(AmbiguousError):
            w.resolve_recipient("Xavi")
        assert {c["name"] for c in w.find_contacts("Xavi")["contacts"]} >= {"Xavi Maria", "Xavier Lopes"}
        assert w.resolve_recipient("Xavi Maria")["phone"] == "+5561944445555"  # full name still wins


def test_local_address_book_number_does_not_become_a_country_code(data_dir):
    lid = "55550000000001@lid"
    _add_contacts(data_dir, [(62, "Local Number", "Local", "(61) 98888-7777", "5561988887777@s.whatsapp.net", lid)])
    with WhatsApp() as w:
        c = w.find_contacts("Local Number")["contacts"][0]
        assert c["phone"] == "+5561988887777"
        assert w.phone_for_jid(lid) == "5561988887777"


def test_person_jid_needs_digits_before_the_at(wa):
    for bad in ("12&x=y@s.whatsapp.net", "abc@lid", "@s.whatsapp.net"):
        with pytest.raises(InvalidArgument):
            wa.resolve_recipient(bad)


def test_chat_state_does_not_reveal_hidden_chats(data_dir):
    hidden = "5561900000001@s.whatsapp.net"
    with WhatsApp() as w:
        assert w.chat_state([hidden]) is None
    with WhatsApp(include_hidden=True) as w:
        assert w.chat_state([hidden]) is not None
