"""Synthetic SQLite databases with the same schema as WhatsApp (never the real database).

All names, numbers and messages are made up. Some keep accents on purpose, to
test case- and accent-insensitive search."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

APPLE = 978307200
NOW = time.time() - APPLE  # "now" in seconds since 2001


def ago(days: float = 0, hours: float = 0) -> float:
    return NOW - days * 86400 - hours * 3600


CHAT_SCHEMA = """
CREATE TABLE ZWACHATSESSION (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZARCHIVED INTEGER, ZCONTACTABID INTEGER,
  ZFLAGS INTEGER, ZHIDDEN INTEGER, ZIDENTITYVERIFICATIONEPOCH INTEGER, ZIDENTITYVERIFICATIONSTATE INTEGER,
  ZMESSAGECOUNTER INTEGER, ZREMOVED INTEGER, ZSESSIONTYPE INTEGER, ZSPOTLIGHTSTATUS INTEGER,
  ZUNREADCOUNT INTEGER, ZGROUPINFO INTEGER, ZLASTMESSAGE INTEGER, ZPROPERTIES INTEGER,
  ZLASTMESSAGEDATE TIMESTAMP, ZLOCATIONSHARINGENDDATE TIMESTAMP, ZCONTACTIDENTIFIER VARCHAR,
  ZCONTACTJID VARCHAR, ZETAG VARCHAR, ZLASTMESSAGETEXT VARCHAR, ZPARTNERNAME VARCHAR, ZSAVEDINPUT VARCHAR
);
CREATE TABLE ZWAMESSAGE (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCHILDMESSAGESDELIVEREDCOUNT INTEGER,
  ZCHILDMESSAGESPLAYEDCOUNT INTEGER, ZCHILDMESSAGESREADCOUNT INTEGER, ZDATAITEMVERSION INTEGER,
  ZDOCID INTEGER, ZENCRETRYCOUNT INTEGER, ZFILTEREDRECIPIENTCOUNT INTEGER, ZFLAGS INTEGER,
  ZGROUPEVENTTYPE INTEGER, ZISFROMME INTEGER, ZMESSAGEERRORSTATUS INTEGER, ZMESSAGESTATUS INTEGER,
  ZMESSAGETYPE INTEGER, ZSORT INTEGER, ZSPOTLIGHTSTATUS INTEGER, ZSTARRED INTEGER, ZCHATSESSION INTEGER,
  ZGROUPMEMBER INTEGER, ZLASTSESSION INTEGER, ZMEDIAITEM INTEGER, ZMESSAGEINFO INTEGER,
  ZPARENTMESSAGE INTEGER, ZMESSAGEDATE TIMESTAMP, ZSENTDATE TIMESTAMP, ZFROMJID VARCHAR,
  ZMEDIASECTIONID VARCHAR, ZPHASH VARCHAR, ZPUSHNAME VARCHAR, ZSTANZAID VARCHAR, ZTEXT VARCHAR, ZTOJID VARCHAR
);
CREATE TABLE ZWAGROUPMEMBER (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCONTACTABID INTEGER, ZISACTIVE INTEGER,
  ZISADMIN INTEGER, ZSENDERKEYSENT INTEGER, ZCHATSESSION INTEGER, ZRECENTGROUPCHAT INTEGER,
  ZCONTACTIDENTIFIER VARCHAR, ZCONTACTNAME VARCHAR, ZFIRSTNAME VARCHAR, ZMEMBERJID VARCHAR
);
CREATE TABLE ZWAMEDIAITEM (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCLOUDSTATUS INTEGER, ZFILESIZE INTEGER,
  ZMEDIAORIGIN INTEGER, ZMOVIEDURATION INTEGER, ZMESSAGE INTEGER, ZASPECTRATIO FLOAT, ZHACCURACY FLOAT,
  ZLATITUDE FLOAT, ZLONGITUDE FLOAT, ZMEDIAURLDATE TIMESTAMP, ZAUTHORNAME VARCHAR, ZCOLLECTIONNAME VARCHAR,
  ZMEDIALOCALPATH VARCHAR, ZMEDIAURL VARCHAR, ZTHUMBNAILLOCALPATH VARCHAR, ZTITLE VARCHAR, ZVCARDNAME VARCHAR,
  ZVCARDSTRING VARCHAR, ZXMPPTHUMBPATH VARCHAR, ZMEDIAKEY BLOB, ZMETADATA BLOB
);
CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZJID VARCHAR, ZPUSHNAME VARCHAR);
CREATE TABLE ZWACHATPROPERTIES (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZALERTS INTEGER, ZENABLED INTEGER,
  ZCHATSESSION INTEGER, ZMUTEDATE TIMESTAMP, ZSOUNDNAME VARCHAR
);
"""

CONTACTS_SCHEMA = """
CREATE TABLE ZWAADDRESSBOOKCONTACT (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZBUSINESSNAME VARCHAR, ZFULLNAME VARCHAR,
  ZGIVENNAME VARCHAR, ZIDENTIFIER VARCHAR, ZLASTNAME VARCHAR, ZLID VARCHAR, ZPHONENUMBER VARCHAR,
  ZUSERNAME VARCHAR, ZWHATSAPPID VARCHAR
);
"""

LID_SCHEMA = """
CREATE TABLE ZWAZACCOUNT (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCREATEDAT TIMESTAMP, ZCOUNTRYCODE VARCHAR,
  ZDISPLAYNAME VARCHAR, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR, ZUSERNAME VARCHAR
);
"""

CALLS_SCHEMA = """
CREATE TABLE ZWACDCALLEVENT (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZBYTESRECEIVED INTEGER, ZBYTESSENT INTEGER,
  ZISBOTGROUPCALL INTEGER, ZISLIGHTWEIGHT INTEGER, ZOUTCOME INTEGER, Z1CALLEVENTS INTEGER,
  Z_FOK_1CALLEVENTS INTEGER, ZDATE TIMESTAMP, ZDURATION FLOAT, ZCALLIDSTRING VARCHAR,
  ZGROUPCALLCREATORUSERJIDSTRING VARCHAR, ZGROUPJIDSTRING VARCHAR, ZREMINDERID VARCHAR, ZSCHEDULEDID VARCHAR
);
CREATE TABLE ZWAAGGREGATECALLEVENT (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZINCOMING INTEGER, ZMISSED INTEGER,
  ZMISSEDREASON INTEGER, ZVIDEO INTEGER, ZFIRSTDATE TIMESTAMP, ZLINKTOKEN VARCHAR
);
CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZOUTCOME INTEGER, Z1PARTICIPANTS INTEGER,
  Z_FOK_1PARTICIPANTS INTEGER, ZJIDSTRING VARCHAR
);
"""

LABELS_SCHEMA = """
CREATE TABLE ZWASMBLABEL (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCOLORID INTEGER, ZISACTIVE INTEGER,
  ZISIMMUTABLE INTEGER, ZLABELID INTEGER, ZORDERINDEX INTEGER, ZPREDEFINEDID INTEGER, ZTYPE INTEGER,
  ZMUTEENDTIME TIMESTAMP, ZALERTTONEID VARCHAR, ZNAME VARCHAR, ZRINGTONEID VARCHAR,
  ZCHATTHEMEDATA BLOB, ZCHATWALLPAPERDATA BLOB
);
CREATE TABLE ZWASMBLABELEDENTITYPROXY (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZENTITYTYPE INTEGER, ZPROXYID INTEGER,
  ZDATE TIMESTAMP, ZCONTACTJID VARCHAR, ZENTITYKEY VARCHAR
);
CREATE TABLE Z_1PROXIES (Z_1LABELS INTEGER, Z_2PROXIES INTEGER, PRIMARY KEY (Z_1LABELS, Z_2PROXIES));
"""

BACKED_UP_SCHEMA = """
CREATE TABLE list_member(label_id LONG_INT NOT NULL, chat_jid TEXT NOT NULL, PRIMARY KEY (label_id, chat_jid));
CREATE TABLE list_schedule(label_id LONG_INT NOT NULL PRIMARY KEY, enabled BOOL NOT NULL DEFAULT 0,
  mute_outside BOOL NOT NULL DEFAULT 0, hide_outside BOOL NOT NULL DEFAULT 0, unmuted_until LONG_INT, unhidden_until LONG_INT);
"""

GROUP_JID = "120363000000000001@g.us"
ANA_JID = "5561911112222@s.whatsapp.net"
BRUNO_LID = "88880000000001@lid"
BRUNO_PHONE = "5511933334444"
DANI_JID = "5561955556666@s.whatsapp.net"
EDU_LID = "99990000000001@lid"
CARLA_JID = "5521977778888@s.whatsapp.net"


def _chat(conn, pk, jid, name, stype=0, *, hidden=0, removed=0, unread=0, archived=0, lastdate=None, lastmsg=None):
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZHIDDEN, ZREMOVED, "
        "ZUNREADCOUNT, ZARCHIVED, ZLASTMESSAGEDATE, ZLASTMESSAGE, ZFLAGS) VALUES (?,?,?,?,?,?,?,?,?,?,256)",
        (pk, jid, name, stype, hidden, removed, unread, archived, lastdate, lastmsg),
    )


def _msg(conn, pk, chat, date, text, *, me=0, mtype=0, fromjid=None, member=None, push=None):
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZISFROMME, ZMESSAGETYPE, "
        "ZFROMJID, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pk, chat, date, text, me, mtype, fromjid, member, push, f"STANZA{pk}"),
    )


def build_dbs(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(base / "ChatStorage.sqlite")
    c.executescript(CHAT_SCHEMA)

    _chat(c, 1, ANA_JID, "Ana Lúcia", 0, unread=2, lastdate=ago(hours=1))
    _chat(c, 2, BRUNO_LID, "Bruno", 0, lastdate=ago(days=1))
    _chat(c, 3, GROUP_JID, "Família", 1, unread=5, lastdate=ago(hours=2))
    _chat(c, 4, "5561900000001@s.whatsapp.net", "Secret", 0, hidden=1, lastdate=ago(hours=3))
    _chat(c, 5, "5561900000002@s.whatsapp.net", "Removed", 0, removed=1, lastdate=ago(hours=4))
    _chat(c, 6, "status@broadcast", None, 3, lastdate=ago(hours=0.5))
    # Pinned: ZLASTMESSAGEDATE in the far future; the real date comes from ZLASTMESSAGE.
    _chat(c, 7, CARLA_JID, "Carla", 0, lastdate=NOW + 86400 * 365 * 6000, lastmsg=700)
    _chat(c, 8, "5561922223333@s.whatsapp.net", "Ana Paula", 0, lastdate=ago(days=3))
    _chat(c, 9, "120363000000000099@newsletter", "News X", 5, lastdate=ago(days=4))

    # Ana (one-to-one)
    _msg(c, 100, 1, ago(days=2), "Hi, how are you?", fromjid=ANA_JID)
    _msg(c, 101, 1, ago(days=2) + 60, "All good!", me=1)
    _msg(c, 102, 1, ago(hours=5), "Coffee at the CAFÉ tomorrow?", fromjid=ANA_JID)
    _msg(c, 103, 1, ago(hours=1), None, mtype=1, fromjid=ANA_JID)  # image without a caption
    _msg(c, 104, 1, ago(hours=1), "100% sure_ok", fromjid=ANA_JID)  # same date as 103
    # Bruno (LID)
    _msg(c, 200, 2, ago(days=1), "Meeting at 10am", fromjid=BRUNO_LID)
    # Group
    c.execute("INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (1, 3, ?, 'Dani from the group')", (DANI_JID,))
    c.execute("INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (2, 3, ?, NULL)", (EDU_LID,))
    c.execute("INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) VALUES (1, ?, 'Edu Push')", (EDU_LID,))
    _msg(c, 300, 3, ago(days=3), "Good morning, family!", fromjid=GROUP_JID, member=1, push="Dani push")
    _msg(c, 301, 3, ago(days=3) + 10, None, mtype=6, fromjid=GROUP_JID)  # system event
    _msg(c, 302, 3, ago(hours=2), "Sunday brunch at Café da Manhã", fromjid=GROUP_JID, member=2, push="Edu msg")
    _msg(c, 303, 3, ago(hours=2) + 5, "I'll bring the bread", me=1, fromjid=None)
    # Hidden / removed
    _msg(c, 400, 4, ago(hours=3), "secret café", fromjid="5561900000001@s.whatsapp.net")
    _msg(c, 500, 5, ago(hours=4), "removed café", fromjid="5561900000002@s.whatsapp.net")
    _msg(c, 600, 6, ago(hours=1), "café on status", fromjid="5561911112222@s.whatsapp.net")
    _msg(c, 700, 7, ago(days=10), "Hi from Carla", fromjid=CARLA_JID)
    _msg(c, 800, 8, ago(days=3), "Hi from Ana Paula", fromjid="5561922223333@s.whatsapp.net")
    c.commit()
    c.close()

    c = sqlite3.connect(base / "ContactsV2.sqlite")
    c.executescript(CONTACTS_SCHEMA)
    rows = [
        ("Ana Lúcia Souza", "Ana", "+5561911112222", ANA_JID, "77770000000001@lid"),
        ("Bruno Lima", "Bruno", f"+{BRUNO_PHONE}", f"{BRUNO_PHONE}@s.whatsapp.net", BRUNO_LID),
        ("Daniela Rocha", "Daniela", "+5561955556666", DANI_JID, "66660000000001@lid"),
        ("Ana Paula", "Ana", "+5561922223333", "5561922223333@s.whatsapp.net", None),
    ]
    for i, (full, given, phone, wa, lid) in enumerate(rows, 1):
        c.execute(
            "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZFULLNAME, ZGIVENNAME, ZPHONENUMBER, ZWHATSAPPID, ZLID) VALUES (?,?,?,?,?,?)",
            (i, full, given, phone, wa, lid),
        )
    c.commit()
    c.close()

    c = sqlite3.connect(base / "CallHistory.sqlite")
    c.executescript(CALLS_SCHEMA)
    # 1: outgoing, answered (Ana, voice); 2: incoming, missed (Bruno, video)
    c.execute("INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZINCOMING, ZMISSED, ZVIDEO, ZFIRSTDATE) VALUES (1, 0, 0, 0, ?)", (ago(days=1),))
    c.execute("INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZINCOMING, ZMISSED, ZVIDEO, ZFIRSTDATE) VALUES (2, 1, 1, 1, ?)", (ago(hours=3),))
    c.execute("INSERT INTO ZWACDCALLEVENT (Z_PK, ZOUTCOME, Z1CALLEVENTS, ZDATE, ZDURATION) VALUES (1, 0, 1, ?, 62.5)", (ago(days=1),))
    c.execute("INSERT INTO ZWACDCALLEVENT (Z_PK, ZOUTCOME, Z1CALLEVENTS, ZDATE, ZDURATION) VALUES (2, 1, 2, ?, 0)", (ago(hours=3),))
    c.execute("INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, ZOUTCOME, Z1PARTICIPANTS, ZJIDSTRING) VALUES (1, 0, 1, ?)", (ANA_JID,))
    c.execute("INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, ZOUTCOME, Z1PARTICIPANTS, ZJIDSTRING) VALUES (2, 1, 2, ?)", (BRUNO_LID,))
    c.commit()
    c.close()

    c = sqlite3.connect(base / "Labels.sqlite")
    c.executescript(LABELS_SCHEMA)
    c.commit()
    c.close()
    (base / "BackedUpStorageDB").mkdir(exist_ok=True)
    c = sqlite3.connect(base / "BackedUpStorageDB" / "BackedUpStorageDatabase.sqlite")
    c.executescript(BACKED_UP_SCHEMA)
    c.commit()
    c.close()

    c = sqlite3.connect(base / "LID.sqlite")
    c.executescript(LID_SCHEMA)
    c.execute("INSERT INTO ZWAZACCOUNT (Z_PK, ZIDENTIFIER, ZPHONENUMBER) VALUES (1, ?, ?)", (BRUNO_LID, BRUNO_PHONE))
    c.execute("INSERT INTO ZWAZACCOUNT (Z_PK, ZIDENTIFIER, ZPHONENUMBER) VALUES (2, ?, '5547900001111')", (EDU_LID,))
    c.commit()
    c.close()
    return base


class _NoCompanion:
    """Companion client that never connects (the older tests keep going through the app UI)."""

    url = "http://127.0.0.1:9"
    secret = None

    def health(self):
        return False

    def request(self, *a, **kw):
        from wa.companion import CompanionUnavailable

        raise CompanionUnavailable("companion turned off in tests")

    results = request


@pytest.fixture(autouse=True)
def _isolate_companion(tmp_path, monkeypatch):
    """No test touches the Keychain, the real GoWA or ~/.local/share/wa-mac."""
    from wa import cli, mcp_server

    monkeypatch.setenv("WA_MAC_HOME", str(tmp_path / "wa-mac-home"))
    monkeypatch.setenv("WA_GOWA_URL", "http://127.0.0.1:9")
    for key in ("WA_ALLOW_ACTIONS", "WA_COMPANION_MAX_PER_MINUTE", "WA_COMPANION_MAX_PER_DAY",
                "WA_COMPANION_DESKTOP_CHECK_S", "WA_GOWA_DIR", "WA_GOWA_PORT", "WA_MEDIA_OUTBOX", "WA_ALLOW_ANY_FILE",
                "WA_MEDIA_CACHE_DIR", "WA_MEDIA_IMAGE_MAX_BYTES", "WA_TRANSCRIBE_ENGINE", "WA_TRANSCRIBE_MODEL",
                "WA_TRANSCRIBE_LANGUAGE", "WA_TRANSCRIBE_REMOTE", "WA_WHISPER_CPP_MODEL", "WA_OCR_LANGUAGES",
                "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli, "_companion_factory", lambda: _NoCompanion())
    monkeypatch.setattr(mcp_server, "_companion_factory", lambda: _NoCompanion())

    # A client built from an env dict without WA_GOWA_URL must not reach a real GoWA on
    # this Mac or read the real Keychain.
    import subprocess

    from wa import companion

    real_default_client = companion.default_client

    def safe_default_client(env=None, runner=None):
        env = dict(os.environ if env is None else env)
        env.setdefault("WA_GOWA_URL", "http://127.0.0.1:9")
        no_keychain = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="")  # noqa: E731
        return real_default_client(env, runner or no_keychain)

    monkeypatch.setattr(companion, "default_client", safe_default_client)

    # The real transcribers on this Mac (mlx_whisper, whisper.cpp) are never run by the tests:
    # tests that need one pass a fake through Tools(which=...).
    from wa import extract

    real_find = extract.Tools.find

    def safe_find(self, name):
        if self.which is None and name in ("mlx_whisper", "whisper-cli", "whisper-cpp"):
            return None
        return real_find(self, name)

    monkeypatch.setattr(extract.Tools, "find", safe_find)


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    base = build_dbs(tmp_path / "wa")
    monkeypatch.setenv("WA_DATA_DIR", str(base))
    monkeypatch.delenv("WA_ALLOW_SEND", raising=False)
    monkeypatch.delenv("WA_SHORTCUT_NAME", raising=False)
    return base


@pytest.fixture
def wa(data_dir):
    from wa.core import WhatsApp

    with WhatsApp() as w:
        yield w
