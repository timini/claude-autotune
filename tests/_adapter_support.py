"""Shared fixtures for the Claude CLI adapter and budget tests."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autotune import config, store

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
REPO_ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 3, 4, 12, 0, 0, tzinfo=timezone.utc)

def open_db(db_path) -> sqlite3.Connection:
    return store.connect(db_path)


def make_cfg(**overrides) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg.setdefault("allow_api_key", False)
    cfg.update(overrides)
    return cfg


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class AdapterTestCase(unittest.TestCase):
    """Temp data dir, a database, and fake CLI environment."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="autotune-adapter-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir()
        self.db_path = self.data_dir / "autotune.db"
        conn = open_db(self.db_path)
        conn.close()
        self.log_path = self.tmp / "fake.log"
        self.script_path = self.tmp / "script.json"
        self.base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.tmp)),
            "AUTOTUNE_FAKE_LOG": str(self.log_path),
            "AUTOTUNE_FAKE_SCRIPT": str(self.script_path),
        }
        self.write_script([{"structured_output": {"ok": True}, "result": "done"}])

    def conn_factory(self) -> sqlite3.Connection:
        return open_db(self.db_path)

    def write_script(self, responses: list[dict]) -> None:
        self.script_path.write_text(json.dumps({"responses": responses}), encoding="utf-8")
        idx = Path(str(self.script_path) + ".idx")
        if idx.exists():
            idx.unlink()

    def fake_calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def model_calls(self) -> list[sqlite3.Row]:
        conn = self.conn_factory()
        try:
            return conn.execute("SELECT * FROM model_calls ORDER BY started_at, call_id").fetchall()
        finally:
            conn.close()


def python_argv(code: str) -> list[str]:
    return [sys.executable, "-c", code]
