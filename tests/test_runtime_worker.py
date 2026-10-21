"""Worker loop run in-process with injected handlers and clock."""

from __future__ import annotations

import json
import sqlite3
import time
import unittest

from autotune import capture, diskcap, ingress, locks, paths, queue, store, worker
from autotune.util import parse_iso, utc_now
from autotune.worker import HandlerResult

from tests._runtime_support import T0, FakeClock, RuntimeCase, envelope, write_config


class WorkerCase(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_dir_path = paths.project_data_dir(ingress.git_facts(self.repo)["git_common_dir"])
        self.conn = self.db()
        self.clock = FakeClock()

    def enqueue(self, job_type="review", key=None, payload=None) -> int:
        job_id, created = queue.enqueue(self.conn, job_type, key or f"{job_type}:{time.monotonic_ns()}",
                                        payload or {}, T0)
        self.assertTrue(created)
        return job_id

    def job(self, job_id):
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def run_worker(self, handlers, **kw):
        kw.setdefault("idle_seconds", 5)
        kw.setdefault("sleep", self.clock.sleep)
        kw.setdefault("clock", self.clock)
        return worker.run_worker(self.repo, handlers=handlers, **kw)

    def log_lines(self) -> list[dict]:
        path = paths.log_path(self.data_dir_path)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


class LifecycleTest(WorkerCase):
    def test_idle_exit_with_no_work_releases_lock(self):
        calls = []
        start = self.clock()
        rc = self.run_worker({"review": lambda ctx, job: calls.append(job["id"])}, idle_seconds=5)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertGreaterEqual((self.clock() - start).total_seconds(), 5)
        self.assertFalse(locks.is_locked(paths.worker_lock_path(self.data_dir_path)))

    def test_second_worker_exits_when_lock_held(self):
        job_id = self.enqueue()
        lock = locks.FileLock(paths.worker_lock_path(self.data_dir_path))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        called = []
        rc = self.run_worker({"review": lambda ctx, job: called.append(1)})
        self.assertEqual(rc, 0)
        self.assertEqual(called, [])
        self.assertEqual(self.job(job_id)["state"], "queued")
        self.assertEqual(self.job(job_id)["attempts"], 0)

    def test_unenrolled_project_exits_cleanly(self):
        (self.repo / ".autotune.json").unlink()
        self.assertEqual(self.run_worker({}), 0)

    def test_invalid_config_is_an_error_exit(self):
        write_config(self.repo, {"enabled": True, "unknown_key": True})
        self.assertNotEqual(self.run_worker({}), 0)

    def test_drains_queue_in_priority_order_then_idles(self):
        seen = []
        a = self.enqueue("propose")
        b = self.enqueue("review")
        c = self.enqueue("review")

        def record(ctx, job):
            seen.append(job["id"])
            return HandlerResult("succeeded", {"n": len(seen)})

        rc = self.run_worker({"review": record, "propose": record})
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [b, c, a])
        for jid in (a, b, c):
            self.assertEqual(self.job(jid)["state"], "succeeded")
            self.assertIsNone(self.job(jid)["lease_owner"])
        self.assertEqual(json.loads(self.job(c)["result_json"]), {"n": 2})

    def test_max_jobs_limits_work(self):
        a, b = self.enqueue(), self.enqueue()
        self.run_worker({"review": lambda ctx, job: HandlerResult("succeeded")}, max_jobs=1)
        self.assertEqual(self.job(a)["state"], "succeeded")
        self.assertEqual(self.job(b)["state"], "queued")

    def test_context_exposes_project_and_injected_clock(self):
        seen = {}

        def handler(ctx, job):
            seen.update(project=ctx.project_dir, now=ctx.now(), cfg=ctx.config["lease_seconds"],
                        data_dir=ctx.data_dir, worker_id=ctx.worker_id)
            return HandlerResult("succeeded")

        self.enqueue()
        self.run_worker({"review": handler})
        self.assertEqual(seen["project"], str(self.repo))
        self.assertEqual(seen["now"].year, T0.year)
        self.assertEqual(seen["cfg"], 300)
        self.assertEqual(seen["data_dir"], self.data_dir_path)
        self.assertTrue(seen["worker_id"])


class SigtermTest(WorkerCase):
    def test_sigterm_during_a_model_call_stops_its_process_group(self):
        import os
        import signal
        import sys
        from pathlib import Path

        from autotune import claude_cli

        job_id = self.enqueue()
        pid_file = Path(self.data_dir_path) / "child.pid"
        code = ("import os, subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pid_file) + '.tmp'!r}, 'w').write(f'{{os.getpid()}} {{p.pid}}'); "
                f"os.replace({str(pid_file) + '.tmp'!r}, {str(pid_file)!r}); time.sleep(60)")

        class SignalOnce:
            sent = False

            def is_set(self):
                if pid_file.exists() and not self.sent:
                    self.sent = True
                    os.kill(os.getpid(), signal.SIGTERM)
                return False

        def handler(ctx, job):
            claude_cli.run_process([sys.executable, "-c", code], cwd=str(self.repo), env=dict(os.environ),
                                   stdin_text="", timeout=30, cancel_event=SignalOnce())
            return HandlerResult("succeeded")

        previous = signal.getsignal(signal.SIGTERM)
        rc = self.run_worker({"review": handler})
        self.assertEqual(rc, 143)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        self.assertFalse(locks.is_locked(paths.worker_lock_path(self.data_dir_path)))
        child, grandchild = (int(p) for p in pid_file.read_text().split())
        for pid in (child, grandchild):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"process {pid} survived SIGTERM of the worker")
        # The job keeps its lease and is reclaimed later like any crashed run.
        self.assertEqual(self.job(job_id)["state"], "leased")


class PauseTest(WorkerCase):
    def test_paused_worker_dispatches_nothing(self):
        jobs = [self.enqueue(t) for t in ("review", "propose", "evaluate", "consolidate", "apply")]
        store.set_meta(self.conn, "paused", "1")
        called = []
        handler = lambda ctx, job: called.append(job["job_type"]) or HandlerResult("succeeded")  # noqa: E731
        rc = self.run_worker({t: handler for t in ("review", "propose", "evaluate", "consolidate", "apply")})
        self.assertEqual(rc, 0)
        self.assertEqual(called, [])
        for jid in jobs:
            self.assertEqual(self.job(jid)["state"], "queued")
            self.assertEqual(self.job(jid)["attempts"], 0)

    def test_resume_dispatches_again(self):
        jid = self.enqueue()
        store.set_meta(self.conn, "paused", "1")
        self.run_worker({"review": lambda ctx, job: HandlerResult("succeeded")})
        self.assertEqual(self.job(jid)["state"], "queued")
        store.set_meta(self.conn, "paused", "0")
        self.run_worker({"review": lambda ctx, job: HandlerResult("succeeded")})
        self.assertEqual(self.job(jid)["state"], "succeeded")

    def test_pause_takes_effect_between_jobs(self):
        a, b = self.enqueue(), self.enqueue()

        def pause_then_succeed(ctx, job):
            other = store.connect(ctx.db_path)
            try:
                store.set_meta(other, "paused", "1")
            finally:
                other.close()
            return HandlerResult("succeeded")

        self.run_worker({"review": pause_then_succeed})
        self.assertEqual(self.job(a)["state"], "succeeded")
        self.assertEqual(self.job(b)["state"], "queued")


class OutcomeTest(WorkerCase):
    def test_transient_exception_becomes_retry_wait(self):
        jid = self.enqueue()

        def flaky(ctx, job):
            raise TimeoutError("model call timed out")

        self.assertEqual(self.run_worker({"review": flaky}), 0)
        row = self.job(jid)
        self.assertEqual(row["state"], "retry_wait")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNone(row["lease_owner"])
        self.assertTrue(row["last_error"])
        self.assertGreater(row["next_eligible_at"], row["updated_at"])

    def test_retries_stop_at_max_attempts(self):
        write_config(self.repo, {"enabled": True, "max_attempts": 2})
        jid = self.enqueue()
        calls = []

        def flaky(ctx, job):
            calls.append(job["attempts"])
            raise TimeoutError("again")

        self.run_worker({"review": flaky})
        self.assertEqual(self.job(jid)["state"], "retry_wait")
        self.clock.advance(7200)
        self.run_worker({"review": flaky})
        self.assertEqual(self.job(jid)["state"], "failed")
        self.assertEqual(calls, [1, 2])

    def test_permanent_exception_fails_job(self):
        jid = self.enqueue()
        self.run_worker({"review": lambda ctx, job: (_ for _ in ()).throw(ValueError("bad data"))})
        row = self.job(jid)
        self.assertEqual(row["state"], "failed")
        self.assertIsNone(row["lease_owner"])

    def test_environment_exception_blocks_job(self):
        jid = self.enqueue()

        def missing(ctx, job):
            raise FileNotFoundError("claude")

        self.run_worker({"review": missing})
        self.assertEqual(self.job(jid)["state"], "blocked_environment")

    def test_handler_result_statuses_map_to_job_states(self):
        cases = {
            "succeeded": "succeeded",
            "retry": "retry_wait",
            "blocked_budget": "blocked_budget",
            "blocked_auth": "blocked_auth",
            "blocked_environment": "blocked_environment",
            "failed": "failed",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                jid = self.enqueue(key=f"k-{status}")
                self.run_worker({"review": lambda ctx, job, s=status: HandlerResult(s, None, f"code_{s}", "d")})
                row = self.job(jid)
                self.assertEqual(row["state"], expected)
                if status != "succeeded":
                    self.assertEqual(row["last_error"], f"code_{status}")

    def test_invalid_handler_return_fails_job(self):
        jid = self.enqueue()
        self.run_worker({"review": lambda ctx, job: {"status": "succeeded"}})
        self.assertEqual(self.job(jid)["state"], "failed")

    def test_unknown_job_type_stays_queued(self):
        orphan = self.enqueue("consolidate")
        known = self.enqueue("review")
        self.run_worker({"review": lambda ctx, job: HandlerResult("succeeded")})
        self.assertEqual(self.job(known)["state"], "succeeded")
        row = self.job(orphan)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)

    def test_placeholder_review_handler_blocks_as_unavailable(self):
        # Never dispatch through the module HANDLERS here: once review.py is
        # present that is the real reviewer, which would call the CLI.
        jid = self.enqueue("review")
        self.run_worker({"review": worker.unavailable_handler})
        row = self.job(jid)
        self.assertEqual(row["state"], "blocked_environment")
        self.assertEqual(row["last_error"], "handler_unavailable")


class HeartbeatTest(WorkerCase):
    def test_long_handler_keeps_its_lease(self):
        write_config(self.repo, {"enabled": True, "lease_seconds": 1})
        jid = self.enqueue()
        stolen = []

        def slow(ctx, job):
            time.sleep(1.6)
            other = store.connect(ctx.db_path)
            try:
                stolen.append(queue.lease(other, "intruder", utc_now(), 60))
            finally:
                other.close()
            return HandlerResult("succeeded")

        rc = worker.run_worker(self.repo, handlers={"review": slow}, idle_seconds=0, heartbeat_seconds=0.2)
        self.assertEqual(rc, 0)
        self.assertEqual(stolen, [None])
        self.assertEqual(self.job(jid)["state"], "succeeded")


class LogTest(WorkerCase):
    def test_log_has_ids_and_states_but_no_payload_text(self):
        jid = self.enqueue(payload={"note": "PAYLOAD_SECRET_TEXT"})

        def handler(ctx, job):
            ctx.log("custom", job_id=job["id"], blob={"x": "NESTED_SECRET"})
            raise TimeoutError("DETAIL_SECRET_TEXT")

        self.run_worker({"review": handler})
        lines = self.log_lines()
        text = paths.log_path(self.data_dir_path).read_text()
        for secret in ("PAYLOAD_SECRET_TEXT", "DETAIL_SECRET_TEXT", "NESTED_SECRET"):
            self.assertNotIn(secret, text)
        ends = [l for l in lines if l["event"] == "job_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["job_id"], jid)
        self.assertEqual(ends[0]["state"], "retry_wait")
        self.assertIsInstance(ends[0]["duration_ms"], int)


class RetentionTest(WorkerCase):
    def test_worker_applies_retention(self):
        env = capture.parse_envelope(json.dumps(envelope("turn_complete", self.repo)).encode())
        ep, evidence = capture.normalize_episode(env, ingress.git_facts(self.repo), T0)
        store.ingest_turn_complete(self.conn, ep, evidence, T0)
        self.clock.advance(40 * 86400)
        self.run_worker({}, idle_seconds=0)
        row = self.conn.execute("SELECT COUNT(*) FROM evidence WHERE content IS NOT NULL").fetchone()
        self.assertEqual(row[0], 0)


if __name__ == "__main__":
    unittest.main()


class LockContentionTest(WorkerCase):
    def test_locked_database_during_lease_does_not_end_the_worker(self):
        import sqlite3
        from unittest import mock

        jid = self.enqueue()
        real = queue.lease
        calls = {"n": 0}

        def flaky_lease(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **kw)

        with mock.patch.object(queue, "lease", flaky_lease):
            rc = self.run_worker({"review": lambda ctx, job: HandlerResult("succeeded")})
        self.assertEqual(rc, 0)
        self.assertEqual(self.job(jid)["state"], "succeeded")
        log = paths.log_path(self.data_dir_path).read_text()
        self.assertIn('"event":"db_locked"', log)

    def test_other_operational_errors_still_propagate(self):
        import sqlite3
        from unittest import mock

        self.enqueue()
        with mock.patch.object(queue, "lease", side_effect=sqlite3.OperationalError("no such table: jobs")):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_worker({"review": lambda ctx, job: HandlerResult("succeeded")})


class ExperimentLockTest(WorkerCase):
    def test_machine_lock_is_shared_across_projects(self):
        ctx_a = worker.Context(db_path=None, data_dir=self.data_dir_path, project_dir=str(self.repo), config={},
                               worker_id="a", clock=self.clock, logger=worker.JsonLog(None, self.clock))
        ctx_b = worker.Context(db_path=None, data_dir=self.home / "other", project_dir="/elsewhere", config={},
                               worker_id="b", clock=self.clock, logger=worker.JsonLog(None, self.clock))
        with ctx_a.experiment_lock() as held_a:
            self.assertTrue(held_a)
            with ctx_b.experiment_lock() as held_b:
                self.assertFalse(held_b)
        with ctx_b.experiment_lock() as held_b:
            self.assertTrue(held_b)
        self.assertEqual(paths.machine_lock_path().parent, self.home)


class CommitOutcomeTest(WorkerCase):
    def meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def test_commit_lands_with_success(self):
        jid = self.enqueue()

        def handler(ctx, job):
            def commit(conn):
                conn.execute("INSERT INTO meta (key, value) VALUES ('review_row', ?)", (str(job["id"]),))
            return HandlerResult("succeeded", {"done": True}, commit=commit)

        self.run_worker({"review": handler})
        self.assertEqual(self.job(jid)["state"], "succeeded")
        self.assertEqual(self.meta("review_row"), str(jid))

    def test_failing_commit_rolls_back_and_retries(self):
        jid = self.enqueue()

        def handler(ctx, job):
            def commit(conn):
                conn.execute("INSERT INTO meta (key, value) VALUES ('review_row', 'partial')")
                raise sqlite3.IntegrityError("constraint failed")
            return HandlerResult("succeeded", commit=commit)

        self.run_worker({"review": handler}, max_jobs=1)
        row = self.job(jid)
        self.assertEqual(row["state"], "retry_wait")
        self.assertEqual(row["last_error"], "commit_failed")
        self.assertIsNone(self.meta("review_row"))

    def test_commit_never_runs_after_the_lease_is_lost(self):
        jid = self.enqueue()
        ran = []

        def handler(ctx, job):
            other = store.connect(ctx.db_path)
            try:
                other.execute("UPDATE jobs SET lease_owner = 'someone-else' WHERE id = ?", (job["id"],))
            finally:
                other.close()
            return HandlerResult("succeeded", commit=lambda conn: ran.append(1))

        self.run_worker({"review": handler}, max_jobs=1)
        self.assertEqual(ran, [])
        self.assertEqual(self.job(jid)["state"], "leased")

    def test_retry_after_defers_without_spending_attempts(self):
        jid = self.enqueue("propose")
        handler = lambda ctx, job: HandlerResult("retry", None, "candidate_slot_busy", "busy",  # noqa: E731
                                                 retry_after_seconds=300)
        self.run_worker({"propose": handler}, max_jobs=1)
        row = self.job(jid)
        self.assertEqual(row["state"], "retry_wait")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["last_error"], "candidate_slot_busy")
        self.assertGreaterEqual((parse_iso(row["next_eligible_at"]) - T0).total_seconds(), 300)

    def test_context_carries_job_identity_and_plugin_root(self):
        jid = self.enqueue()
        seen = {}

        def handler(ctx, job):
            seen.update(job_id=ctx.job_id, attempt=ctx.attempt, root=ctx.plugin_root, claude=ctx.claude,
                        event=ctx.cancel_event.is_set())
            return HandlerResult("succeeded")

        self.run_worker({"review": handler})
        self.assertEqual(seen["job_id"], jid)
        self.assertEqual(seen["attempt"], 1)
        self.assertTrue((seen["root"] / "autotune" / "worker.py").is_file())
        self.assertIsNone(seen["claude"])
        self.assertFalse(seen["event"])


class CancelRunningTest(WorkerCase):
    def request_stop(self, ctx, job_id):
        other = store.connect(ctx.db_path)
        try:
            self.assertTrue(queue.cancel(other, job_id, utc_now()))
        finally:
            other.close()

    def test_stop_request_reaches_a_running_handler(self):
        jid = self.enqueue()
        seen = []

        def handler(ctx, job):
            self.request_stop(ctx, job["id"])
            seen.append(ctx.cancel_event.wait(10))
            return HandlerResult("cancelled", None, "cancelled", "stopped by request")

        start = time.monotonic()
        self.run_worker({"review": handler}, heartbeat_seconds=0.05)
        self.assertEqual(seen, [True])
        self.assertLess(time.monotonic() - start, 5)
        row = self.job(jid)
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["last_error"], "cancelled")

    def test_non_success_after_stop_request_is_recorded_as_cancelled(self):
        jid = self.enqueue()

        def handler(ctx, job):
            self.request_stop(ctx, job["id"])
            return HandlerResult("retry", None, "timeout", "would retry")

        self.run_worker({"review": handler}, max_jobs=1)
        self.assertEqual(self.job(jid)["state"], "cancelled")

    def test_success_after_stop_request_stands_with_its_commit(self):
        jid = self.enqueue()

        def handler(ctx, job):
            self.request_stop(ctx, job["id"])
            return HandlerResult("succeeded", commit=lambda conn: conn.execute(
                "INSERT INTO meta (key, value) VALUES ('committed', '1')"))

        self.run_worker({"review": handler}, max_jobs=1)
        self.assertEqual(self.job(jid)["state"], "succeeded")
        self.assertEqual(store.get_meta(self.conn, "committed"), "1")


class BudgetUnblockTest(WorkerCase):
    def test_budget_blocks_from_earlier_days_lift_at_start(self):
        old = self.enqueue(key="old")
        today = self.enqueue(key="today")
        auth = self.enqueue(key="auth")
        yesterday = "2026-01-01T08:00:00.000Z"
        this_morning = "2026-01-02T01:00:00.000Z"
        for jid, state, ts in ((old, "blocked_budget", yesterday), (today, "blocked_budget", this_morning),
                               (auth, "blocked_auth", yesterday)):
            self.conn.execute("UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?", (state, ts, jid))
        self.clock.t = parse_iso("2026-01-02T09:00:00.000Z")
        ran = []
        self.run_worker({"review": lambda ctx, job: ran.append(job["id"]) or HandlerResult("succeeded")})
        self.assertEqual(ran, [old])
        self.assertEqual(self.job(today)["state"], "blocked_budget")
        self.assertEqual(self.job(auth)["state"], "blocked_auth")

    def test_budget_blocks_lift_when_the_day_changes_mid_run(self):
        jid = self.enqueue()
        self.clock.t = parse_iso("2026-01-01T23:59:58.000Z")

        def handler(ctx, job):
            return HandlerResult("blocked_budget", None, "budget_exhausted", "limit")

        calls = []

        def counting(ctx, job):
            calls.append(ctx.now())
            return handler(ctx, job)

        self.run_worker({"review": counting}, idle_seconds=5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].date().isoformat(), "2026-01-02")
        self.assertEqual(self.job(jid)["state"], "blocked_budget")


class LogRotationTest(unittest.TestCase):
    def test_rotation_keeps_the_configured_number_of_files(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autotune.log"
            log = worker.JsonLog(path, utc_now, max_bytes=500, keep=2)
            for i in range(200):
                log.write("event", n=i)
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(names, ["autotune.log", "autotune.log.1", "autotune.log.2"])
            for p in Path(tmp).iterdir():
                self.assertLess(p.stat().st_size, 600)
            last = json.loads(path.read_text().splitlines()[-1])
            self.assertEqual(last["n"], 199)
            first_of_1 = json.loads((Path(tmp) / "autotune.log.1").read_text().splitlines()[0])
            first_of_current = json.loads(path.read_text().splitlines()[0])
            self.assertLess(first_of_1["n"], first_of_current["n"])

    def test_no_rotation_without_a_size_limit(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autotune.log"
            log = worker.JsonLog(path, utc_now, max_bytes=None)
            for i in range(50):
                log.write("event", n=i)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["autotune.log"])


class DiskCapWorkerTest(WorkerCase):
    def test_worker_enforces_the_cap_at_start(self):
        from tests._store_support import make_episode, make_evidence

        write_config(self.repo, {"enabled": True, "max_data_mb": 1})
        for i in range(30):
            ep = make_episode(session_id="s", turn_id=f"t{i}", now=T0)
            store.ingest_turn_complete(self.conn, ep, make_evidence((f"req {i}", "y" * 100_000)), T0)
        self.assertTrue(diskcap.over_cap(self.data_dir_path, {"max_data_mb": 1}))
        self.run_worker({}, idle_seconds=0)
        self.assertFalse(diskcap.over_cap(self.data_dir_path, {"max_data_mb": 1}))
        expired = self.conn.execute("SELECT COUNT(*) FROM evidence WHERE expired_at IS NOT NULL").fetchone()[0]
        self.assertGreater(expired, 0)
        events = [l["event"] for l in self.log_lines()]
        self.assertIn("disk_cap_enforced", events)
