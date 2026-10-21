"""Ingress under the data cap and when the store cannot be written."""

from __future__ import annotations

import json
import sqlite3
import stat
from unittest import mock

from autotune import diskcap

from tests._runtime_support import RuntimeCase, envelope, write_config


class DiskCapIngressTest(RuntimeCase):
    def fallback(self) -> list[dict]:
        path = diskcap.fallback_path(self.data_dir())
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_over_cap_turn_is_refused_and_logged_outside_the_store(self):
        self.ingest_ack(envelope("turn_start", self.repo, turn_id="t1"))
        write_config(self.repo, {"enabled": True, "max_data_mb": 1})
        logs = self.data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "autotune.log").write_bytes(b"x" * (2 * 1024 * 1024))

        code, ack = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t1"))
        self.assertEqual(code, 3)
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "disk_cap")
        self.assertEqual(ack["worker"], "disabled")
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM capture_errors").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT state FROM foreground_activity").fetchone()[0], "idle")
        recs = self.fallback()
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["code"], recs[0]["session_id"], recs[0]["turn_id"]),
                         ("disk_cap", "sess-1", "t1"))
        mode = stat.S_IMODE(diskcap.fallback_path(self.data_dir()).stat().st_mode)
        self.assertEqual(mode, 0o600)

        (logs / "autotune.log").unlink()
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t1"))
        self.assertEqual(code, 0)
        self.assertFalse(ack["duplicate"])

    def test_under_cap_turn_is_stored(self):
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 0)
        self.assertEqual(self.fallback(), [])

    def test_full_disk_capture_error_goes_to_the_fallback_file(self):
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch("autotune.store.ingest_turn_complete", side_effect=full), \
                mock.patch("autotune.store.record_capture_error", side_effect=full):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t-full"))
        self.assertEqual(code, 3)
        self.assertEqual(ack["error"], "persistence_failed")
        recs = self.fallback()
        self.assertEqual([(r["code"], r["turn_id"]) for r in recs], [("persistence_failed", "t-full")])

    def test_other_store_errors_do_not_use_the_fallback_file(self):
        boom = sqlite3.OperationalError("disk I/O error")
        with mock.patch("autotune.store.ingest_turn_complete", side_effect=boom), \
                mock.patch("autotune.store.record_capture_error", side_effect=boom):
            code, _ = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 3)
        self.assertEqual(self.fallback(), [])
