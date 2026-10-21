"""Crash recovery and lost-wakeup handling in the worker."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from unittest import mock

from autotune import locks, paths, queue, store, worker
from autotune.util import utc_now
from tests import _integration_handlers as handlers
from tests._integration_support import IntegrationCase, wait_for


def _run_crashing_worker(repo: str) -> None:
    worker.run_worker(repo, handlers={"review": handlers.crash_once_review}, idle_seconds=0, max_jobs=1)


class CrashRecoveryTest(IntegrationCase):
    repo_config = {"enabled": True, "lease_seconds": 2}

    def test_killed_worker_job_is_reclaimed_and_completed_once(self):
        code, ack, out, _ = self.run_ingress(self.envelope(session_id="cr-s", turn_id="cr-t"))
        self.assertEqual(code, 0, out)
        job_id = ack["job_id"]

        ctx = multiprocessing.get_context("spawn")
        with mock.patch.dict(os.environ, self.sub_env()):
            proc = ctx.Process(target=_run_crashing_worker, args=(str(self.repo),))
            proc.start()
        self.track_pid(proc.pid)
        self.assertTrue(wait_for(lambda: handlers.count_lines(self.test_dir, "starts"), 10.0),
                        "worker never started the job")
        row = self.query("SELECT state, lease_owner FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual(row["state"], "leased")
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(5)
        self.assertEqual(proc.exitcode, -signal.SIGKILL)
        self.assertEqual(handlers.count_lines(self.test_dir, "completions"), [])

        # The kernel dropped the flock with the process.
        self.assertFalse(locks.is_locked(paths.worker_lock_path(self.data_dir())))
        time.sleep(2.2)  # past lease_seconds

        with mock.patch.dict(os.environ, self.sub_env()):
            rc = worker.run_worker(self.repo, handlers={"review": handlers.crash_once_review},
                                   idle_seconds=0)
            self.assertEqual(rc, 0)
            # A further worker finds nothing left to do.
            worker.run_worker(self.repo, handlers={"review": handlers.crash_once_review}, idle_seconds=0)

        self.assertEqual(handlers.count_lines(self.test_dir, "completions"), [str(job_id)])
        self.assertEqual(handlers.count_lines(self.test_dir, "starts"), [f"{job_id}:1", f"{job_id}:2"])
        row = self.query("SELECT state, attempts, lease_owner FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual((row["state"], row["attempts"], row["lease_owner"]), ("succeeded", 2, None))


class LostWakeupTest(IntegrationCase):
    def test_job_enqueued_during_idle_exit_is_still_processed(self):
        """Ingress commits a job, probes the lock and sees the worker still
        holding it, so it spawns nobody. The exiting worker must pick it up."""
        code, ack, out, _ = self.run_ingress(self.envelope(session_id="lw-s", turn_id="lw-t0"))
        self.assertEqual(code, 0, out)
        db = paths.db_path(self.data_dir())
        done = []
        injected = []

        def handler(ctx, job):
            done.append(job["id"])
            return worker.HandlerResult("succeeded")

        real_lease = queue.lease
        empty_after_work = []

        def lease(*a, **kw):
            job = real_lease(*a, **kw)
            if job is None and done:
                empty_after_work.append(True)
            return job

        def clock():
            # The first clock call after an empty lease is the idle check that
            # leads to exit. Enqueue at that moment, from another connection.
            if empty_after_work and not injected:
                conn = store.connect(db)
                try:
                    with store.transaction(conn):
                        job_id, _ = queue.enqueue(conn, "review", "review:late", {}, utc_now())
                    injected.append(job_id)
                finally:
                    conn.close()
            return utc_now()

        with mock.patch.dict(os.environ, self.sub_env()), mock.patch.object(queue, "lease", lease):
            rc = worker.run_worker(self.repo, handlers={"review": handler}, idle_seconds=0, clock=clock)
        self.assertEqual(rc, 0)
        self.assertEqual(len(injected), 1)
        self.assertEqual(done, [ack["job_id"], injected[0]])
