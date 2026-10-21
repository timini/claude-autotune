"""Crash recovery at every application and revert boundary.

A real worker process runs the job with AUTOTUNE_TEST_FAULT naming one fault
point and dies there with exit code 70 (os._exit, so no cleanup runs). After
its lease expires a second worker, without the fault, finishes the job. There
must be exactly one application (or revert) commit, HEAD must be that commit
and the candidate must be recorded as applied (or reverted).
"""

from __future__ import annotations

import multiprocessing
import os
import time
import unittest
from unittest import mock

from autotune import faults, git_ops, queue, worker
from autotune.util import utc_now
from tests._cycle2_support import BASE_CLAUDE_MD, USEFUL_CLAUDE_MD, Cycle2Case, git, snapshot

LEASE_SECONDS = 2


def _faulty_worker(repo: str) -> None:
    worker.run_worker(repo, idle_seconds=0, poll_seconds=0.05)


class CrashCase(Cycle2Case):
    repo_config = {"enabled": True, "lease_seconds": LEASE_SECONDS}

    def crash_at(self, point: str) -> None:
        env = {"AUTOTUNE_TEST_FAULT": point, "AUTOTUNE_TEST_FAULTS_ENABLED": "1"}
        ctx = multiprocessing.get_context("spawn")
        with mock.patch.dict(os.environ, env):
            proc = ctx.Process(target=_faulty_worker, args=(str(self.repo),))
            proc.start()
        proc.join(60)
        if proc.is_alive():
            proc.kill()
            self.fail(f"worker did not stop at {point}")
        self.assertEqual(proc.exitcode, faults.FAULT_EXIT_CODE, f"worker did not crash at {point}")
        time.sleep(LEASE_SECONDS + 0.3)

    def recover(self) -> None:
        self.assertNotIn("AUTOTUNE_TEST_FAULT", os.environ)
        rc = worker.run_worker(str(self.repo), idle_seconds=0, poll_seconds=0.05)
        self.assertEqual(rc, 0)

    def accepted(self) -> dict:
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD}, status="accepted", evaluated=True)
        queue.enqueue(self.conn, "apply", f"apply:{cand['candidate_id']}", {"candidate_id": cand["candidate_id"]},
                      utc_now(), candidate_id=cand["candidate_id"])
        return cand

    def assert_applied_once(self, cand: dict) -> None:
        cid = cand["candidate_id"]
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), cand["candidate_commit"])
        self.assertEqual(git_ops.find_trailer_commits(self.repo, cid, refs=["refs/heads/main"]),
                         [cand["candidate_commit"]])
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["applied_commit"]), ("applied", cand["candidate_commit"]))
        jobs = [j for j in self.jobs("apply") if j["candidate_id"] == cid]
        self.assertEqual([j["state"] for j in jobs], ["succeeded"], jobs)
        self.assertEqual(len([j for j in self.jobs("verify_active") if j["candidate_id"] == cid]), 1)


class ApplyCrashTest(CrashCase):
    def test_kill_at_each_application_boundary_recovers_without_duplicate(self):
        for point in ("apply_before_merge", "apply_after_merge", "apply_after_record"):
            with self.subTest(point):
                if point != "apply_before_merge":
                    git(self.repo, "reset", "-q", "--hard", self.base)
                cand = self.accepted()
                self.crash_at(point)
                at_crash = git(self.repo, "rev-parse", "HEAD")
                self.assertEqual(at_crash, self.base if point == "apply_before_merge" else cand["candidate_commit"])
                self.recover()
                self.assert_applied_once(cand)


class RevertCrashTest(CrashCase):
    def test_kill_at_each_revert_boundary_recovers_without_duplicate(self):
        for point in ("revert_after_commit", "revert_after_merge"):
            with self.subTest(point):
                git(self.repo, "reset", "-q", "--hard", self.base)
                cand = self.accepted()
                cid = cand["candidate_id"]
                self.recover()
                self.assertEqual(self.candidate(cid)["status"], "applied")
                queue.enqueue(self.conn, "reproduce_regression", f"reproduce_regression:{cid}:manual",
                              {"candidate_id": cid, "mode": "revert_now"}, utc_now(), candidate_id=cid)
                self.crash_at(point)
                self.recover()

                reverts = git_ops.find_trailer_commits_for(self.repo, git_ops.REVERT_TRAILER_KEY, cid,
                                                           refs=["refs/heads/main"])
                self.assertEqual(len(reverts), 1)
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), reverts[0])
                self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), cand["candidate_commit"])
                self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "3")
                self.assertEqual((self.repo / "CLAUDE.md").read_text(), BASE_CLAUDE_MD)
                row = self.candidate(cid)
                self.assertEqual((row["status"], row["reverted_commit"]), ("reverted", reverts[0]))
                self.assertEqual(git(self.repo, "branch", "--list", "autotune/revert-*"), "")
                self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")


class FaultGuardTest(CrashCase):
    def test_fault_points_need_the_explicit_test_switch(self):
        cand = self.accepted()
        before = snapshot(self.repo)
        with mock.patch.dict(os.environ, {"AUTOTUNE_TEST_FAULT": "apply_before_merge"}):
            rc = worker.run_worker(str(self.repo), idle_seconds=0, poll_seconds=0.05)
        self.assertEqual(rc, 0)
        self.assertNotEqual(snapshot(self.repo)["head"], before["head"])
        self.assert_applied_once(cand)


if __name__ == "__main__":
    unittest.main()
