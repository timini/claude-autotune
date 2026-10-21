"""Fixtures for integration tests: real subprocesses, a temp Git repo and a
temp AUTOTUNE_HOME."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from autotune import locks, paths
from tests._runtime_support import envelope, git, make_repo

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class IntegrationCase(unittest.TestCase):
    repo_config: dict | None = {"enabled": True}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        self.test_dir = self.root / "effects"
        self.test_dir.mkdir()
        self.repo = make_repo(self.root, self.repo_config)
        self._pids: list[int] = []
        # Registered after tmp.cleanup, so it runs first: no stray worker
        # keeps writing into a directory being removed.
        self.addCleanup(self._kill_strays)

    def _kill_strays(self) -> None:
        pid = locks.holder_pid(paths.worker_lock_path(self.data_dir()))
        for p in [*self._pids, *([pid] if pid else [])]:
            if p != os.getpid() and pid_alive(p):
                try:
                    os.kill(p, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def track_pid(self, pid: int) -> None:
        self._pids.append(pid)

    def sub_env(self, *, worker: bool = False, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AUTOTUNE_")}
        env["AUTOTUNE_HOME"] = str(self.home)
        env["AUTOTUNE_TEST_DIR"] = str(self.test_dir)
        # A worker spawned with the default handlers must never reach a real
        # model: point it at a CLI that does not exist.
        env["AUTOTUNE_CLAUDE"] = str(self.test_dir / "no-such-claude")
        if not worker:
            env["AUTOTUNE_NO_WORKER"] = "1"
        env.update(extra)
        return env

    def envelope(self, kind: str = "turn_complete", **kw) -> dict:
        return envelope(kind, self.repo, **kw)

    def run_ingress(self, payload, env: dict | None = None, timeout: float = 5.0):
        """Returns (exit code, ack dict or None, stdout text, seconds)."""
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        start = time.monotonic()
        proc = subprocess.run(
            [PYTHON, "-m", "autotune", "ingress"],
            input=raw,
            cwd=PLUGIN_ROOT,
            env=self.sub_env() if env is None else env,
            capture_output=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        out = proc.stdout.decode("utf-8", "replace")
        lines = out.splitlines()
        ack = json.loads(lines[0]) if len(lines) == 1 else None
        return proc.returncode, ack, out, elapsed

    def run_cli(self, *args, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-m", "autotune", *args],
            cwd=PLUGIN_ROOT,
            env=self.sub_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def data_dir(self) -> Path:
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return self.home / paths.project_id(common)

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        db = paths.db_path(self.data_dir())
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def scalar(self, sql: str, params=()):
        return self.query(sql, params)[0][0]
