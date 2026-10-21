"""Shared fixtures for the store tests."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autotune import store
from autotune.util import canonical_json, iso, new_id, sha256_text

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_episode(session_id="s1", turn_id="t1", now=T0, **extra) -> dict:
    row = {
        "episode_id": new_id("ep"),
        "session_id": session_id,
        "turn_id": turn_id,
        "lifecycle_id": "lc1",
        "lifecycle_source": "startup",
        "origin": "foreground",
        "task_id": None,
        "started_at": iso(now),
        "completed_at": iso(now),
        "captured_at": iso(now),
        "end_reason": "answer",
        "duration_ms": 10,
        "model": "m",
        "transcript_path": None,
        "repository_json": canonical_json({}),
        "loaded_harness_json": canonical_json({"instructions": [], "skills": [], "exposure": "unknown"}),
        "coverage_json": canonical_json({}),
        "usage_json": None,
    }
    row.update(extra)
    return row


def make_evidence(texts=("please fix", "done")) -> list[dict]:
    rows = []
    for i, text in enumerate(texts, start=1):
        rows.append(
            {
                "evidence_id": f"e{i:04d}",
                "kind": "user_request" if i == 1 else "assistant_answer",
                "role": "user" if i == 1 else "assistant",
                "content": text,
                "content_sha256": sha256_text(text),
                "provenance_json": canonical_json({"source": "turn.complete"}),
                "position": i,
                "truncated": 0,
            }
        )
    return rows


class TempDBCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autotune-store-"))
        self.db_path = self.tmp / "autotune.sqlite3"
        self.conn = store.connect(self.db_path)
        self._extra = []

    def tearDown(self):
        for c in [self.conn, *self._extra]:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def second_connection(self):
        c = store.connect(self.db_path)
        self._extra.append(c)
        return c

    def ingest(self, session_id="s1", turn_id="t1", now=T0, texts=("please fix", "done"), **extra):
        return store.ingest_turn_complete(
            self.conn, make_episode(session_id, turn_id, now, **extra), make_evidence(texts), now
        )

    def job(self, job_id, conn=None):
        return (conn or self.conn).execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
