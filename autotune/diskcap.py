"""Disk usage of a project data dir, the data cap, and the fallback error file.

The cap covers the database (with its WAL and shared memory files) and the
logs. Candidate worktrees are reported but not capped: they are removed when
their candidate finishes.

When the store cannot be written (over the cap, or the disk is full), capture
errors go to a small JSON lines file next to the database instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import paths
from .util import iso

FALLBACK_FILENAME = "capture-errors.jsonl"
FALLBACK_MAX_BYTES = 1024 * 1024
WORKTREES_DIRNAME = "worktrees"
ENFORCE_TARGET_FRACTION = 0.9


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dir_files_size(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _tree_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _capped_parts(data_dir: Path) -> dict:
    db = paths.db_path(data_dir)
    return {
        "db": _size(db),
        "db_wal": _size(db.with_name(db.name + "-wal")),
        "db_shm": _size(db.with_name(db.name + "-shm")),
        "logs": _dir_files_size(Path(data_dir) / "logs"),
    }


def capped_bytes(data_dir) -> int:
    """Bytes that count toward max_data_mb. A few stat calls; safe on the hook path."""
    return sum(_capped_parts(Path(data_dir)).values())


def usage(data_dir) -> dict:
    data_dir = Path(data_dir)
    out = _capped_parts(data_dir)
    out["capped_total"] = sum(out.values())
    wt = data_dir / WORKTREES_DIRNAME
    out["worktrees"] = _tree_size(wt) if wt.is_dir() else 0
    out["total"] = out["capped_total"] + out["worktrees"]
    return out


def cap_bytes(cfg: dict) -> int:
    return int(cfg["max_data_mb"]) * 1024 * 1024


def over_cap(data_dir, cfg: dict) -> bool:
    return capped_bytes(data_dir) > cap_bytes(cfg)


def fallback_path(data_dir) -> Path:
    return Path(data_dir) / FALLBACK_FILENAME


def append_fallback_error(data_dir, code: str, session_id, turn_id, now: datetime, detail=None) -> bool:
    """Append one line to the fallback file. Best effort; returns False on failure."""
    path = fallback_path(data_dir)
    rec = {"code": code, "session_id": session_id, "turn_id": turn_id, "ts": iso(now)}
    if detail:
        rec["detail"] = str(detail)[:200]
    line = (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if _size(path) + len(line) > FALLBACK_MAX_BYTES:
            flags |= os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def fallback_count(data_dir) -> int:
    try:
        with open(fallback_path(data_dir), "rb") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def is_disk_full(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.Error):
        return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL or "full" in str(exc).lower()
    return isinstance(exc, OSError)
