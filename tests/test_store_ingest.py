import os
import stat
import unittest
from datetime import timedelta

from autotune import queue, store
from autotune.util import parse_iso

from tests._store_support import T0, TempDBCase, make_episode, make_evidence


class ConnectTest(TempDBCase):
    def test_schema_applied_and_pragmas(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"episodes", "evidence", "jobs", "meta", "capture_errors"} <= tables)

    def test_file_is_private(self):
        mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_reconnect_keeps_data(self):
        self.ingest()
        self.conn.close()
        self.conn = store.connect(self.db_path)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with store.transaction(self.conn):
                self.conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
                raise RuntimeError("boom")
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(store.get_meta(self.conn, "k"))


class IngestTest(TempDBCase):
    def test_first_ingest_creates_episode_evidence_and_review_job(self):
        res = self.ingest()
        self.assertFalse(res["duplicate"])
        ep = self.conn.execute("SELECT * FROM episodes").fetchone()
        self.assertEqual(ep["episode_id"], res["episode_id"])
        self.assertEqual(ep["review_status"], "pending")
        ev = self.conn.execute(
            "SELECT * FROM evidence WHERE episode_id = ? ORDER BY position", (res["episode_id"],)
        ).fetchall()
        self.assertEqual([r["content"] for r in ev], ["please fix", "done"])
        for r in ev:
            self.assertEqual(parse_iso(r["retain_until"]), T0 + timedelta(days=30))
        job = self.job(res["job_id"])
        self.assertEqual(job["job_type"], "review")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["episode_id"], res["episode_id"])
        self.assertEqual(job["priority"], 10)

    def test_duplicate_returns_same_ids(self):
        first = self.ingest()
        later = T0 + timedelta(minutes=5)
        again = store.ingest_turn_complete(
            self.conn, make_episode(now=later), make_evidence(("other", "text")), later
        )
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["episode_id"], first["episode_id"])
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        contents = [r[0] for r in self.conn.execute("SELECT content FROM evidence ORDER BY position")]
        self.assertEqual(contents, ["please fix", "done"])

    def test_distinct_turns_are_distinct_episodes(self):
        a = self.ingest(turn_id="t1")
        b = self.ingest(turn_id="t2")
        self.assertNotEqual(a["episode_id"], b["episode_id"])
        self.assertNotEqual(a["job_id"], b["job_id"])

    def test_custom_raw_retention(self):
        res = self.ingest(_raw_retention_days=3)
        r = self.conn.execute("SELECT retain_until FROM evidence LIMIT 1").fetchone()
        self.assertEqual(parse_iso(r[0]), T0 + timedelta(days=3))
        self.assertFalse(res["duplicate"])

    def test_failed_ingest_leaves_nothing(self):
        bad = make_evidence()
        bad[1]["evidence_id"] = bad[0]["evidence_id"]  # primary key clash
        with self.assertRaises(Exception):
            store.ingest_turn_complete(self.conn, make_episode(), bad, T0)
        for table in ("episodes", "evidence", "jobs"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertFalse(self.ingest()["duplicate"])


class MiscStoreTest(TempDBCase):
    def test_record_activity_upserts(self):
        store.record_activity(self.conn, "s1", "active", "t1", T0)
        store.record_activity(self.conn, "s1", "idle", "t1", T0 + timedelta(seconds=1))
        rows = self.conn.execute("SELECT * FROM foreground_activity").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "idle")

    def test_record_capture_error(self):
        store.record_capture_error(self.conn, None, None, "invalid_json", "bad", T0)
        row = self.conn.execute("SELECT * FROM capture_errors").fetchone()
        self.assertEqual(row["code"], "invalid_json")
        self.assertIsNone(row["session_id"])

    def test_meta_roundtrip(self):
        self.assertIsNone(store.get_meta(self.conn, "paused"))
        store.set_meta(self.conn, "paused", "1")
        store.set_meta(self.conn, "paused", "0")
        self.assertEqual(store.get_meta(self.conn, "paused"), "0")

    def test_queue_counts(self):
        counts = store.queue_counts(self.conn)
        self.assertEqual(
            counts, {"queued": 0, "leased": 0, "retry_wait": 0, "blocked": 0, "failed": 0, "succeeded": 0}
        )
        ids = [queue.enqueue(self.conn, "propose", f"p{i}", {}, T0)[0] for i in range(6)]
        w = "w"
        leased = [queue.lease(self.conn, w, T0, 60) for _ in range(5)]
        queue.complete(self.conn, leased[0]["id"], w, T0, None)
        queue.retry_later(self.conn, leased[1]["id"], w, T0, "timeout", None, max_attempts=5)
        queue.block(self.conn, leased[2]["id"], w, T0, "blocked_auth", "auth_required", None)
        queue.block(self.conn, leased[3]["id"], w, T0, "blocked_budget", "usage_limit", None)
        queue.fail(self.conn, leased[4]["id"], w, T0, "permanent", None)
        queue.enqueue(self.conn, "propose", "p_leased", {}, T0)
        queue.lease(self.conn, w, T0, 60)
        self.assertEqual(len(ids), 6)
        self.assertEqual(
            store.queue_counts(self.conn),
            {"queued": 1, "leased": 1, "retry_wait": 1, "blocked": 2, "failed": 1, "succeeded": 1},
        )


if __name__ == "__main__":
    unittest.main()


class ActivityOrderingTest(TempDBCase):
    def state(self):
        row = self.conn.execute("SELECT state, turn_id FROM foreground_activity WHERE session_id = 's1'").fetchone()
        return tuple(row)

    def test_completion_of_earlier_turn_does_not_idle_a_running_turn(self):
        store.record_activity(self.conn, "s1", "active", "A", T0)
        store.record_activity(self.conn, "s1", "active", "B", T0 + timedelta(seconds=2))
        store.record_activity(self.conn, "s1", "idle", "A", T0 + timedelta(seconds=3))
        self.assertEqual(self.state(), ("active", "B"))
        store.record_activity(self.conn, "s1", "idle", "B", T0 + timedelta(seconds=4))
        self.assertEqual(self.state(), ("idle", "B"))

    def test_late_events_after_session_end_are_ignored(self):
        store.record_activity(self.conn, "s1", "active", "A", T0)
        store.record_activity(self.conn, "s1", "ended", None, T0 + timedelta(seconds=5))
        store.record_activity(self.conn, "s1", "active", "A", T0 + timedelta(seconds=1))
        store.record_activity(self.conn, "s1", "idle", "A", T0 + timedelta(seconds=6))
        self.assertEqual(self.state()[0], "ended")

    def test_older_event_does_not_overwrite_newer(self):
        store.record_activity(self.conn, "s1", "active", "B", T0 + timedelta(seconds=10))
        store.record_activity(self.conn, "s1", "active", "A", T0)
        self.assertEqual(self.state(), ("active", "B"))

    def test_new_turn_after_end_with_later_time_reactivates(self):
        store.record_activity(self.conn, "s1", "ended", None, T0)
        store.record_activity(self.conn, "s1", "active", "C", T0 + timedelta(seconds=1))
        self.assertEqual(self.state(), ("active", "C"))
