"""Fixtures for the runtime (ingress, worker, cli) tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from autotune import ingress, paths, store

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_GIT_ID = ["-c", "user.name=Autotune Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo, *args) -> str:
    out = subprocess.run(["git", *_GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def make_repo(root: Path, config: dict | None = None) -> Path:
    """A Git repo with one commit. `config` is written to .autotune.json unless None."""
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    if config is not None:
        write_config(repo, config)
    return Path(os.path.realpath(repo))


def write_config(repo: Path, config: dict) -> None:
    (repo / ".autotune.json").write_text(json.dumps(config))


def envelope(kind: str, cwd, session_id="sess-1", turn_id="turn-1", **extra) -> dict:
    env = {
        "schema": "autotune.envelope/1",
        "kind": kind,
        "origin": "foreground",
        "session_id": session_id,
        "cwd": str(cwd),
        "sent_at": "2026-01-01T12:00:00.000Z",
    }
    if kind == "turn_start":
        env.update(turn_id=turn_id, request_text="please fix the bug", lifecycle_id="lc-1",
                   lifecycle_source="startup")
    elif kind == "session_end":
        env.update(reason="prompt_input_exit")
    elif kind == "turn_complete":
        env.update(
            turn_id=turn_id,
            lifecycle_id="lc-1",
            lifecycle_source="startup",
            transcript_path=None,
            started_at="2026-01-01T11:59:00.000Z",
            completed_at="2026-01-01T12:00:00.000Z",
            end_reason="answer",
            refusal=None,
            duration_ms=60000,
            usage=None,
            model="test-model",
            request_text="please fix the bug",
            answer_text="I fixed it.",
            tool_calls=[],
            skills=[],
            instructions=[],
            coverage={"truncated_fields": [], "dropped_tool_calls": 0, "missing_turn_start": False,
                      "capture_errors": []},
        )
    env.update(extra)
    return env


class RuntimeCase(unittest.TestCase):
    """Temp AUTOTUNE_HOME, worker spawn disabled, and an enrolled temp repo."""

    repo_config: dict | None = {"enabled": True}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"
        # AUTOTUNE_CLAUDE names a CLI that does not exist, so nothing here can reach a real model.
        self.env = {"AUTOTUNE_HOME": str(self.home), "AUTOTUNE_NO_WORKER": "1",
                    "AUTOTUNE_CLAUDE": str(self.root / "no-such-claude")}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AUTOTUNE_ORIGIN", None)
        self.repo = make_repo(self.root, self.repo_config)

    def ingest(self, obj, env: dict | None = None) -> tuple[int, list[str]]:
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        out = io.StringIO()
        code = ingress.main(io.BytesIO(raw), out, dict(os.environ) if env is None else env)
        return code, out.getvalue().splitlines()

    def ingest_ack(self, obj, env: dict | None = None) -> tuple[int, dict]:
        code, lines = self.ingest(obj, env)
        self.assertEqual(len(lines), 1, f"expected exactly one stdout line, got {lines!r}")
        return code, json.loads(lines[0])

    def data_dir(self) -> Path:
        return self.home / paths.project_id(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))

    def db(self):
        conn = store.connect(paths.db_path(self.data_dir()))
        self.addCleanup(conn.close)
        return conn


class FakeClock:
    """Deterministic clock; `sleep` advances it instead of waiting."""

    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=max(seconds, 0.001))

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
