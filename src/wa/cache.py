"""Cache for OCR, transcripts and reduced images, in ~/.local/share/wa-mac/cache.

Entries are keyed by the SHA-256 of the media file plus the settings that change the
result (engine, model, language). The folder is 700 and every file 600. Nothing in
it is needed: `wa media-cache --clear` deletes it all.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Mapping


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    from .companion import base_dir

    env = os.environ if env is None else env
    raw = env.get("WA_MEDIA_CACHE_DIR")
    return Path(raw).expanduser() if raw else base_dir(env) / "cache"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _key(file_hash: str, op: str, params: Mapping[str, object]) -> str:
    raw = json.dumps([file_hash, op, dict(sorted(params.items()))], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def write_private(path: Path, data: bytes) -> None:
    """Write a file readable only by the owner (600), replacing it atomically."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Cache:
    def __init__(self, env: Mapping[str, str] | None = None, enabled: bool = True):
        self.root = cache_dir(env)
        self.enabled = enabled

    def _path(self, file_hash: str, op: str, params: Mapping[str, object]) -> Path:
        return ensure_dir(self.root) / f"{op}-{_key(file_hash, op, params)}.json"

    def get(self, file_hash: str, op: str, params: Mapping[str, object]) -> dict | None:
        if not self.enabled:
            return None
        path = self.root / f"{op}-{_key(file_hash, op, params)}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, file_hash: str, op: str, params: Mapping[str, object], value: dict) -> None:
        if not self.enabled:
            return
        write_private(self._path(file_hash, op, params), json.dumps(value, ensure_ascii=False).encode())

    def image_path(self, file_hash: str, label: str) -> Path:
        d = ensure_dir(ensure_dir(self.root) / "images")
        return d / f"{file_hash[:32]}-{label}.jpg"


def stats(env: Mapping[str, str] | None = None) -> dict:
    root = cache_dir(env)
    files = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    return {"path": str(root), "entries": len(files), "bytes": sum(p.stat().st_size for p in files)}


def clear(env: Mapping[str, str] | None = None) -> dict:
    before = stats(env)
    root = cache_dir(env)
    if root.exists():
        shutil.rmtree(root)
    return {"cleared": True, "path": before["path"], "removed_entries": before["entries"], "freed_bytes": before["bytes"]}
