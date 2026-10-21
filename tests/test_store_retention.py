import unittest
from datetime import timedelta

from autotune import queue, retention, store
from autotune.util import iso

from tests._store_support import T0, TempDBCase


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class ApplyRetentionTest(TempDBCase):
    def test_nothing_expires_early(self):
        self.ingest()
        res = retention.apply_retention(self.conn, T0 + timedelta(days=29), 30, 180)
        self.assertEqual(res["evidence_expired"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM evidence WHERE content IS NOT NULL").fetchone()[0], 2
        )

    def test_expired_content_nulled_rows_kept_and_unreviewed_marked(self):
        res = self.ingest()
        later = T0 + timedelta(days=31)
        out = retention.apply_retention(self.conn, later, 30, 180)
        self.assertEqual(out["evidence_expired"], 2)
        self.assertEqual(out["episodes_evidence_expired"], 1)
        rows = self.conn.execute("SELECT * FROM evidence").fetchall()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertIsNone(r["content"])
            self.assertEqual(r["expired_at"], iso(later))
            self.assertTrue(r["content_sha256"])
        ep = self.conn.execute("SELECT review_status FROM episodes").fetchone()
        self.assertEqual(ep["review_status"], "evidence_expired")
        job = self.job(res["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["last_error"], "evidence_expired")
        self.assertIsNone(queue.lease(self.conn, "w", later, 60))
        # Idempotent.
        again = retention.apply_retention(self.conn, later + timedelta(days=1), 30, 180)
        self.assertEqual(again["evidence_expired"], 0)
        self.assertEqual(again["episodes_evidence_expired"], 0)

    def test_blocked_and_retry_wait_reviews_also_marked(self):
        a = self.ingest(turn_id="a")
        b = self.ingest(turn_id="b")
        queue.lease(self.conn, "w", T0, 60)
        queue.lease(self.conn, "w", T0, 60)
        queue.block(self.conn, a["job_id"], "w", T0, "blocked_auth", "auth_required", None)
        queue.retry_later(self.conn, b["job_id"], "w", T0, "timeout", None, 5)
        out = retention.apply_retention(self.conn, T0 + timedelta(days=31), 30, 180)
        self.assertEqual(out["episodes_evidence_expired"], 2)
        self.assertEqual(self.job(a["job_id"])["state"], "failed")
        self.assertEqual(self.job(b["job_id"])["state"], "failed")

    def test_leased_and_reviewed_episodes_not_marked(self):
        leased = self.ingest(turn_id="leased")
        done = self.ingest(turn_id="done")
        queue.lease(self.conn, "w", T0, 10**7)
        row = queue.lease(self.conn, "w", T0, 60)
        queue.complete(self.conn, row["id"], "w", T0, None)
        retention.apply_retention(self.conn, T0 + timedelta(days=31), 30, 180)
        self.assertEqual(self.job(leased["job_id"])["state"], "leased")
        self.assertEqual(self.job(done["job_id"])["state"], "succeeded")
        statuses = {r[0] for r in self.conn.execute("SELECT review_status FROM episodes")}
        self.assertEqual(statuses, {"pending"})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM evidence WHERE content IS NOT NULL").fetchone()[0], 0
        )

    def test_shortened_raw_days_takes_effect(self):
        self.ingest()
        out = retention.apply_retention(self.conn, T0 + timedelta(days=8), 7, 180)
        self.assertEqual(out["evidence_expired"], 2)

    def test_result_detail_expires(self):
        jid, _ = queue.enqueue(self.conn, "propose", "p", {}, T0)
        queue.lease(self.conn, "w", T0, 60)
        queue.complete(self.conn, jid, "w", T0, {"summary": "x"})
        store.record_capture_error(self.conn, None, None, "invalid_json", "d", T0)
        retention.apply_retention(self.conn, T0 + timedelta(days=100), 30, 180)
        self.assertIsNotNone(self.job(jid)["result_json"])
        out = retention.apply_retention(self.conn, T0 + timedelta(days=181), 30, 180)
        self.assertEqual(out["job_results_cleared"], 1)
        self.assertIsNone(self.job(jid)["result_json"])
        self.assertEqual(self.job(jid)["state"], "succeeded")
        self.assertEqual(count(self.conn, "capture_errors"), 0)


class PurgeTest(TempDBCase):
    def setUp(self):
        super().setUp()
        self.a = self.ingest(turn_id="a", now=T0)
        self.b = self.ingest(turn_id="b", now=T0 + timedelta(days=2))
        self.conn.execute(
            "INSERT INTO reviews (review_id, episode_id, job_id, task_stage, review_json, recommendation, created_at) "
            "VALUES ('rv1', ?, ?, 'done', '{}', 'no_change', ?)",
            (self.a["episode_id"], self.a["job_id"], iso(T0)),
        )
        queue.enqueue(self.conn, "consolidate", "c1", {}, T0)
        store.record_capture_error(self.conn, "s1", "a", "invalid_json", "x", T0)
        store.record_activity(self.conn, "s1", "idle", "b", T0)

    def test_purge_evidence_keeps_rows(self):
        out = retention.purge(self.conn, "evidence", None)
        self.assertEqual(out["evidence"], 4)
        self.assertEqual(count(self.conn, "evidence"), 4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM evidence WHERE content IS NULL AND expired_at IS NOT NULL").fetchone()[0],
            4,
        )
        self.assertEqual(count(self.conn, "episodes"), 2)
        self.assertEqual(count(self.conn, "reviews"), 1)

    def test_purge_all_respects_foreign_keys(self):
        retention.purge(self.conn, "all", None)
        for table in ("episodes", "evidence", "jobs", "reviews", "capture_errors", "foreground_activity"):
            self.assertEqual(count(self.conn, table), 0, table)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        # The store is usable afterwards and the old identity is new again.
        self.assertFalse(self.ingest(turn_id="a")["duplicate"])

    def test_purge_all_before_cutoff(self):
        out = retention.purge(self.conn, "all", T0 + timedelta(days=1))
        self.assertEqual(out["episodes"], 1)
        remaining = [r[0] for r in self.conn.execute("SELECT episode_id FROM episodes")]
        self.assertEqual(remaining, [self.b["episode_id"]])
        self.assertIsNotNone(self.job(self.b["job_id"]))
        self.assertIsNone(self.job(self.a["job_id"]))
        self.assertEqual(count(self.conn, "reviews"), 0)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unknown_scope(self):
        with self.assertRaises(ValueError):
            retention.purge(self.conn, "everything", None)


if __name__ == "__main__":
    unittest.main()
