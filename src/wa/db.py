"""Read-only access to the WhatsApp Desktop (Mac) SQLite databases.

Rules:
- Always `file:...?mode=ro` + `PRAGMA query_only=ON` (reads the WAL, so data is live).
- Never `immutable=1`.
- Busy database: retry after a short wait.
- No Full Disk Access: clear error (`FullDiskAccessError`).
"""

from __future__ import annotations

import os
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

DEFAULT_DATA_DIR = (
    Path.home() / "Library" / "Group Containers" / "group.net.whatsapp.WhatsApp.shared"
)

CHAT_DB = "ChatStorage.sqlite"
CONTACTS_DB = "ContactsV2.sqlite"
LID_DB = "LID.sqlite"
CALLS_DB = "CallHistory.sqlite"
LABELS_DB = "Labels.sqlite"
BACKED_UP_DB = "BackedUpStorageDB/BackedUpStorageDatabase.sqlite"

APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 -> 1970-01-01, in seconds

_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0)


class WAError(Exception):
    """Domain error with a stable code for the CLI and MCP."""

    code = "error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.details:
            out.update(self.details)
        return out


class FullDiskAccessError(WAError):
    code = "full_disk_access_required"


class NotInstalledError(WAError):
    code = "whatsapp_data_not_found"


class DatabaseBusyError(WAError):
    code = "database_busy"


class NotFoundError(WAError):
    code = "not_found"


class AmbiguousError(WAError):
    code = "ambiguous"


class InvalidArgument(WAError):
    code = "invalid_argument"


class SendRefused(WAError):
    code = "send_refused"


class SendFailed(WAError):
    code = "send_failed"


class AccessibilityError(WAError):
    code = "accessibility_required"


class AutomationError(WAError):
    code = "automation_permission_required"


class ActionRefused(WAError):
    code = "action_refused"


class ActionFailed(WAError):
    code = "action_failed"


class MissingDependency(WAError):
    code = "missing_dependency"


FDA_MESSAGE = (
    "No permission to read WhatsApp data. Grant Full Disk Access "
    "(System Settings > Privacy & Security > Full Disk Access) to the "
    "process that runs `wa`: the terminal app (Terminal, iTerm...) or the "
    "app/daemon that starts the MCP server (for example Claude Desktop or Cursor). Then reopen that app."
)


def data_dir() -> Path:
    env = os.environ.get("WA_DATA_DIR")
    return Path(env).expanduser() if env else DEFAULT_DATA_DIR


def _check_readable(path: Path) -> None:
    try:
        with open(path, "rb") as fh:
            fh.read(16)
    except FileNotFoundError:
        parent = path.parent
        try:
            parent_exists = parent.exists() and any(True for _ in os.scandir(parent))
        except PermissionError:
            raise FullDiskAccessError(FDA_MESSAGE, path=str(path)) from None
        raise NotInstalledError(
            "WhatsApp database not found. Is WhatsApp Desktop installed and has it "
            "finished syncing on this Mac?",
            path=str(path),
            dir_exists=bool(parent_exists),
        ) from None
    except PermissionError:
        raise FullDiskAccessError(FDA_MESSAGE, path=str(path)) from None


def normalize(value) -> str:
    """Lowercase, without accents (for search)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def connect(name: str = CHAT_DB, base: Path | None = None) -> sqlite3.Connection:
    """Open a WhatsApp database read-only, retrying while it is busy."""
    path = (base or data_dir()) / name
    _check_readable(path)
    uri = f"file:{quote(str(path))}?mode=ro"
    last: Exception | None = None
    for delay in (0.0, *_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.create_function("wa_norm", 1, normalize, deterministic=True)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            if _is_busy(exc):
                continue
            if "unable to open" in str(exc).lower() or "authorization" in str(exc).lower():
                raise FullDiskAccessError(FDA_MESSAGE, path=str(path)) from None
            raise
    raise DatabaseBusyError(
        "The WhatsApp database is busy; try again in a moment.", detail=str(last)
    )


def query(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Run a query, retrying while the database is busy."""
    last: Exception | None = None
    for delay in (0.0, *_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            last = exc
    raise DatabaseBusyError(
        "The WhatsApp database is busy; try again in a moment.", detail=str(last)
    )
