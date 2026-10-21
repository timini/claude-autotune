"""Disk usage, the data cap, the fallback error file, and cap enforcement."""

from __future__ import annotations

import json
import os
import stat
import unittest
from datetime import timedelta

from autotune import diskcap, paths, retention, store

from tests._store_support import T0, TempDBCase, make_episode, make_evidence

MIB = 1024 * 1024


class UsageTest(TempDBCase):
    def test_usage_counts_database_logs_and_worktrees(self):
        logs = self.tmp / "logs"
        logs.mkdir()
        (logs / "autotune.log").write_bytes(b"x" * 1000)
        (logs / "autotune.log.1").write_bytes(b"x" * 500)
        wt = self.tmp / "worktrees" / "cand_1"
        wt.mkdir(parents=True)
        (wt / "CLAUDE.md").write_bytes(b"y" * 3000)
        u = diskcap.usage(self.tmp)
        self.assertEqual(u["db"], self.db_path.stat().st_size)
        self.assertEqual(u["logs"], 1500)
        self.assertEqual(u["worktrees"], 3000)
        self.assertEqual(u["capped_total"], u["db"] + u["db_wal"] + u["db_shm"] + u["logs"])
        self.assertEqual(u["total"], u["capped_total"] + 3000)

    def test_worktrees_do_not_count_toward_the_cap(self):
        wt = self.tmp / "worktrees" / "cand_1"
        wt.mkdir(parents=True)
        (wt / "big").write_bytes(b"z" * (2 * MIB))
        self.assertFalse(diskcap.over_cap(self.tmp, {"max_data_mb": 1}))
        logs = self.tmp / "logs"
        logs.mkdir()
        (logs / "autotune.log").write_bytes(b"x" * (2 * MIB))
        self.assertTrue(diskcap.over_cap(self.tmp, {"max_data_mb": 1}))

    def test_missing_data_dir_is_zero(self):
        u = diskcap.usage(self.tmp / "nope")
        self.assertEqual(u["total"], 0)


class FallbackTest(TempDBCase):
    def test_lines_are_private_and_counted(self):
        self.assertEqual(diskcap.fallback_count(self.tmp), 0)
        self.assertTrue(diskcap.append_fallback_error(self.tmp, "disk_cap", "s1", "t1", T0))
        self.assertTrue(diskcap.append_fallback_error(self.tmp, "persistence_failed", "s1", "t2", T0, "full"))
        path = diskcap.fallback_path(self.tmp)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        recs = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(recs[0], {"code": "disk_cap", "session_id": "s1", "turn_id": "t1",
                                   "ts": "2026-01-01T12:00:00.000Z"})
        self.assertEqual(diskcap.fallback_count(self.tmp), 2)

    def test_file_is_truncated_rather_than_growing_past_one_mib(self):
        path = diskcap.fallback_path(self.tmp)
        path.write_bytes(b"{}\n" * (diskcap.FALLBACK_MAX_BYTES // 3))
        os.chmod(path, 0o600)
        diskcap.append_fallback_error(self.tmp, "disk_cap", "s", "t", T0)
        self.assertLess(path.stat().st_size, 1000)
        self.assertEqual(diskcap.fallback_count(self.tmp), 1)

    def test_unwritable_location_reports_failure(self):
        self.assertFalse(diskcap.append_fallback_error(self.tmp / "missing" / "dir", "disk_cap", "s", "t", T0))


class EnforceCapTest(TempDBCase):
    def fill(self, n, size, start=T0):
        for i in range(n):
            now = start + timedelta(minutes=i)
            ep = make_episode(session_id="s", turn_id=f"t{i}", now=now)
            store.ingest_turn_complete(self.conn, ep, make_evidence((f"req {i}", "y" * size)), now)

    def live(self):
        return [r["turn_id"] for r in self.conn.execute(
            "SELECT DISTINCT e.turn_id FROM evidence v JOIN episodes e USING (episode_id) "
            "WHERE v.expired_at IS NULL ORDER BY e.completed_at")]

    def test_large_rows_expire_oldest_first_until_under_target(self):
        self.fill(40, 50_000)
        target = MIB
        self.assertGreater(retention.live_bytes(self.conn), target)
        counts = retention.enforce_cap(self.conn, target, T0 + timedelta(days=1))
        self.assertTrue(counts["under_target"])
        self.assertLess(retention.live_bytes(self.conn), target)
        live = self.live()
        self.assertTrue(live, "everything was expired")
        self.assertEqual(live, [f"t{i}" for i in range(40 - len(live), 40)])
        self.assertLess(diskcap.usage(self.tmp)["capped_total"], 2 * MIB)

    def test_small_rows_are_not_all_expired(self):
        # Inline rows free no whole pages, so a loop that only watched page
        # counts would expire everything.
        self.fill(1000, 2000)
        before = retention.live_bytes(self.conn)
        counts = retention.enforce_cap(self.conn, before - 500_000, T0 + timedelta(days=1))
        self.assertTrue(counts["under_target"])
        live = self.live()
        self.assertGreater(len(live), 500)
        self.assertEqual(live[-1], "t999")

    def test_under_target_is_a_no_op(self):
        self.fill(3, 100)
        counts = retention.enforce_cap(self.conn, 100 * MIB, T0)
        self.assertEqual(counts["evidence_expired"], 0)
        self.assertEqual(counts["compactions"], 0)

    def test_nothing_left_to_expire_stops(self):
        self.fill(2, 100)
        counts = retention.enforce_cap(self.conn, 0, T0)
        self.assertFalse(counts["under_target"])
        self.assertEqual(self.live(), [])


if __name__ == "__main__":
    unittest.main()
