import multiprocessing
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from autotune import queue, store

from tests._store_support import T0, make_episode, make_evidence

ROUNDS = 20


def _race_ingest(args):
    db_path, start_at = args
    while time.time() < start_at:
        time.sleep(0.001)
    conn = store.connect(db_path)
    try:
        return store.ingest_turn_complete(conn, make_episode("race-session", "race-turn"), make_evidence(), T0)
    finally:
        conn.close()


def _race_lease(args):
    db_path, worker_id, start_at = args
    while time.time() < start_at:
        time.sleep(0.001)
    conn = store.connect(db_path)
    try:
        row = queue.lease(conn, worker_id, T0, 60)
        return None if row is None else row["id"]
    finally:
        conn.close()


class ProcessRaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="autotune-race-"))
        cls.pool = multiprocessing.get_context("spawn").Pool(2)
        cls.pool.map(abs, [0, 0])  # warm both workers before timing starts

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        cls.pool.join()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_racing_ingest_yields_one_episode_and_one_job(self):
        for i in range(ROUNDS):
            # A fresh file each round also races schema creation.
            db_path = str(self.tmp / f"ingest-{i}.sqlite3")
            start = time.time() + 0.05
            results = self.pool.map(_race_ingest, [(db_path, start), (db_path, start)], chunksize=1)
            self.assertEqual(sorted(r["duplicate"] for r in results), [False, True], f"round {i}")
            self.assertEqual(results[0]["episode_id"], results[1]["episode_id"])
            self.assertEqual(results[0]["job_id"], results[1]["job_id"])
            conn = store.connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
            finally:
                conn.close()

    def test_racing_lease_is_exclusive(self):
        for i in range(ROUNDS):
            db_path = str(self.tmp / f"lease-{i}.sqlite3")
            conn = store.connect(db_path)
            jid, _ = queue.enqueue(conn, "review", "only", {}, T0)
            conn.close()
            start = time.time() + 0.05
            results = self.pool.map(
                _race_lease, [(db_path, "w1", start), (db_path, "w2", start)], chunksize=1
            )
            self.assertEqual(sorted(results, key=lambda x: x is None), [jid, None], f"round {i}")


if __name__ == "__main__":
    unittest.main()
