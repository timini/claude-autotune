"""Application, activation, regression detection and revert (M5 exit criteria)
against a real temporary Git repo. The user's checkout is snapshotted around
every refused or deferred operation."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from autotune import apply, git_ops, locks, store
from autotune.util import canonical_json, iso, new_id, parse_iso, utc_now
from tests._cycle2_support import (
    BASE_CLAUDE_MD, NO_VERIFY_CLAUDE_MD, USEFUL_CLAUDE_MD, Cycle2Case, git, snapshot,
)


class ApplyCase(Cycle2Case):
    def accepted(self, files=None, **cols) -> dict:
        return self.make_candidate(files or {"CLAUDE.md": USEFUL_CLAUDE_MD}, status="accepted", evaluated=True,
                                   **cols)

    def apply(self, cid: str, **kw):
        return self.run_job("apply", cid, **kw)

    def applied(self, files=None) -> dict:
        cand = self.accepted(files)
        res, job = self.apply(cand["candidate_id"])
        self.assertEqual(job["state"], "succeeded", job)
        return self.candidate(cand["candidate_id"])

    def episode(self, *, head: str, completed_at, instructions=(), skills=(), exposure=None) -> str:
        eid = new_id("ep")
        harness = {"instructions": [{"file_path": p} for p in instructions],
                   "skills": [{"skill": Path(p).name, "path": p} for p in skills],
                   "exposure": exposure or ("observed" if instructions or skills else "unknown")}
        repository = {"toplevel": str(self.repo), "branch": "main", "head": head, "tracked_dirty": False}
        with store.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO episodes (episode_id, session_id, turn_id, lifecycle_id, lifecycle_source, origin, "
                "completed_at, captured_at, end_reason, repository_json, loaded_harness_json, coverage_json) "
                "VALUES (?, ?, ?, 'lc', 'startup', 'foreground', ?, ?, 'answer', ?, ?, '{}')",
                (eid, f"s-{eid}", f"t-{eid}", iso(completed_at), iso(completed_at), canonical_json(repository),
                 canonical_json(harness)),
            )
        return eid

    def assert_waiting(self, cid, job, code):
        self.assertEqual(job["state"], "retry_wait", job)
        self.assertEqual(job["last_error"], code)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("waiting_to_apply", code))
        self.assertIsNone(row["applied_commit"])


class ApplyTest(ApplyCase):
    def test_accepted_candidate_is_fast_forwarded_and_recorded(self):
        cand = self.accepted()
        cid = cand["candidate_id"]
        res, job = self.apply(cid)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), cand["candidate_commit"])
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), USEFUL_CLAUDE_MD)
        self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        row = self.candidate(cid)
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["applied_commit"], cand["candidate_commit"])
        self.assertIsNotNone(row["applied_at"])
        self.assertEqual(row["exposure"], "unknown")
        self.assertIsNone(row["worktree_path"])
        self.assertFalse(Path(cand["worktree_path"]).exists())
        self.assertEqual([j["dedup_key"] for j in self.jobs("verify_active")], [f"verify_active:{cid}"])

    def test_existing_application_is_reconciled_without_a_new_commit(self):
        cand = self.accepted()
        cid = cand["candidate_id"]
        git(self.repo, "merge", "--ff-only", "-q", cand["candidate_commit"])
        res, job = self.apply(cid)
        self.assertEqual(job["state"], "succeeded")
        self.assertTrue(res.result["reconciled"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), cand["candidate_commit"])
        self.assertEqual(git_ops.find_trailer_commits(self.repo, cid, refs=["refs/heads/main"]),
                         [cand["candidate_commit"]])
        self.assertEqual(self.candidate(cid)["status"], "applied")
        # A second run finds it applied and does nothing.
        res, job = self.run_job("apply", cid, dedup=f"apply:{cid}:again")
        self.assertEqual(res.result["skipped"], "already_applied")

    def test_dirty_user_work_stays_byte_identical_and_application_waits(self):
        added = ".claude/rules/report.md"
        setups = {
            "modified tracked file": lambda: (self.repo / "src/app.py").write_text("print('mine')\n"),
            "staged change": lambda: ((self.repo / "src/app.py").write_text("print('staged')\n"),
                                      git(self.repo, "add", "src/app.py")),
            "untracked file at an added path": lambda: ((self.repo / ".claude/rules").mkdir(parents=True),
                                                        (self.repo / added).write_text("my notes\n")),
        }
        for name, setup in setups.items():
            with self.subTest(name):
                cand = self.accepted({"CLAUDE.md": USEFUL_CLAUDE_MD, added: "Report failing tests.\n"})
                setup()
                before = snapshot(self.repo)
                res, job = self.apply(cand["candidate_id"])
                self.assert_waiting(cand["candidate_id"], job, "checkout_dirty")
                self.assertEqual(snapshot(self.repo), before)
                self.assertEqual(before["head"], self.base)
                git(self.repo, "reset", "-q", "--hard", self.base)
                git(self.repo, "clean", "-q", "-fd")
                with store.transaction(self.conn):
                    self.conn.execute("UPDATE candidates SET status = 'rejected' WHERE candidate_id = ?",
                                      (cand["candidate_id"],))

    def test_untracked_file_elsewhere_is_left_alone(self):
        (self.repo / "notes.txt").write_text("keep me\n")
        cand = self.accepted()
        res, job = self.apply(cand["candidate_id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual((self.repo / "notes.txt").read_text(), "keep me\n")
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), "?? notes.txt")

    def test_moved_head_makes_the_candidate_stale(self):
        cand = self.accepted()
        (self.repo / "src/app.py").write_text("print('new work')\n")
        git(self.repo, "commit", "-q", "-am", "user work")
        before = snapshot(self.repo)
        res, job = self.apply(cand["candidate_id"])
        self.assertEqual(job["state"], "succeeded")
        row = self.candidate(cand["candidate_id"])
        self.assertEqual((row["status"], row["reason"]), ("stale", "base_changed"))
        self.assertEqual(snapshot(self.repo), before)
        self.assertIsNone(row["applied_commit"])

    def test_changed_branch_waits(self):
        cand = self.accepted()
        git(self.repo, "switch", "-q", "-c", "feature")
        before = snapshot(self.repo)
        res, job = self.apply(cand["candidate_id"])
        self.assert_waiting(cand["candidate_id"], job, "branch_changed")
        self.assertEqual(snapshot(self.repo), before)

    def test_detached_head_waits_even_without_a_target_branch(self):
        # A candidate proposed on a detached HEAD has no target branch; a
        # detached HEAD at apply time must not count as a match.
        for target in ("main", None):
            with self.subTest(target=target):
                cand = self.accepted(target_branch=target)
                git(self.repo, "switch", "-q", "--detach", self.base)
                before = snapshot(self.repo)
                res, job = self.apply(cand["candidate_id"])
                self.assert_waiting(cand["candidate_id"], job, "branch_changed")
                self.assertEqual(snapshot(self.repo), before)
                git(self.repo, "switch", "-q", "main")
                with store.transaction(self.conn):
                    self.conn.execute("UPDATE candidates SET status = 'rejected' WHERE candidate_id = ?",
                                      (cand["candidate_id"],))

    def test_changed_policy_or_evaluator_makes_the_candidate_stale(self):
        for column, value in (("policy_digest", "0" * 64), ("evaluator_commit", canonical_json({"cases_tree": "x"}))):
            with self.subTest(column):
                cand = self.accepted(**{column: value})
                res, job = self.apply(cand["candidate_id"])
                row = self.candidate(cand["candidate_id"])
                self.assertEqual((row["status"], row["reason"]), ("stale", "policy_changed"))
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_known_foreground_activity_defers_and_old_activity_does_not(self):
        cand = self.accepted()
        store.record_activity(self.conn, "sess-old", "active", "t0", utc_now() - timedelta(hours=7))
        store.record_activity(self.conn, "sess-now", "active", "t1", utc_now() - timedelta(minutes=5))
        res, job = self.apply(cand["candidate_id"])
        self.assert_waiting(cand["candidate_id"], job, "foreground_active")
        store.record_activity(self.conn, "sess-now", "idle", "t1", utc_now())
        res, job = self.apply(cand["candidate_id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), cand["candidate_commit"])

    def test_pause_flags_defer_application(self):
        for key, value in (("apply_paused_reason", "regression_suspected:cand_x"),
                           ("git_reconciliation_required", "HEAD differs")):
            with self.subTest(key):
                store.set_meta(self.conn, key, value)
                cand = self.accepted()
                res, job = self.apply(cand["candidate_id"])
                self.assert_waiting(cand["candidate_id"], job, "apply_paused")
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
                with store.transaction(self.conn):
                    self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
                    self.conn.execute("UPDATE candidates SET status = 'rejected' WHERE candidate_id = ?",
                                      (cand["candidate_id"],))

    def test_busy_apply_lock_defers(self):
        cand = self.accepted()
        held = locks.FileLock(apply.apply_lock_path(self.data_dir), blocking=False)
        self.assertTrue(held.acquire())
        try:
            res, job = self.apply(cand["candidate_id"])
        finally:
            held.release()
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "apply_lock_busy"))
        self.assertEqual(self.candidate(cand["candidate_id"])["status"], "accepted")

    def test_missing_target_branch_waits_even_on_a_matching_branch(self):
        cand = self.accepted(target_branch=None)
        before = snapshot(self.repo)
        res, job = self.apply(cand["candidate_id"])
        self.assert_waiting(cand["candidate_id"], job, "branch_changed")
        self.assertEqual(snapshot(self.repo), before)

    def test_git_operation_in_progress_waits(self):
        cand = self.accepted()
        git(self.repo, "bisect", "start")
        self.addCleanup(lambda: git(self.repo, "bisect", "reset"))
        before = snapshot(self.repo)
        res, job = self.apply(cand["candidate_id"])
        self.assert_waiting(cand["candidate_id"], job, "checkout_dirty")
        self.assertIn("in progress", job["last_error_detail"])
        self.assertEqual(snapshot(self.repo), before)

    def test_mismatch_after_the_merge_pauses_application(self):
        from unittest import mock

        cand = self.accepted()
        cid = cand["candidate_id"]
        with mock.patch.object(git_ops, "worktree_matches", return_value=False):
            res, job = self.apply(cid)
        self.assertEqual(job["state"], "succeeded", job)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("applied", "git_reconciliation_required"))
        self.assertIn(cid, store.get_meta(self.conn, "git_reconciliation_required"))
        before = snapshot(self.repo)
        self.assertEqual(before["head"], cand["candidate_commit"])
        other = self.make_candidate({"src/app.py": "print('other')\n"}, status="accepted", evaluated=True)
        res, job = self.apply(other["candidate_id"])
        self.assert_waiting(other["candidate_id"], job, "apply_paused")
        self.assertEqual(snapshot(self.repo), before)

    def test_rejected_candidate_is_never_applied(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD}, status="rejected", evaluated=True)
        res, job = self.apply(cand["candidate_id"])
        self.assertEqual(res.result, {"skipped": "candidate_rejected"})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)


class VerifyActiveTest(ApplyCase):
    def test_no_observed_exposure_leaves_the_candidate_applied(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        # An episode at the applied commit that did not load the changed file,
        # and one that loaded it but ran on the old base.
        later = parse_iso(cand["applied_at"]) + timedelta(minutes=1)
        self.episode(head=cand["applied_commit"], completed_at=later, instructions=[str(self.repo / "README.md")])
        self.episode(head=self.base, completed_at=later, instructions=[str(self.repo / "CLAUDE.md")])
        res, job = self.run_job("verify_active", cid)
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "exposure_unknown"))
        self.assertGreaterEqual(parse_iso(job["next_eligible_at"]) - utc_now(), timedelta(hours=23))
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["exposure"]), ("applied", "unknown"))

    def test_observed_load_of_a_changed_path_marks_observed_active(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        (self.repo / "src/app.py").write_text("print('later')\n")
        git(self.repo, "commit", "-q", "-am", "later work")
        later = parse_iso(cand["applied_at"]) + timedelta(minutes=1)
        self.episode(head=git(self.repo, "rev-parse", "HEAD"), completed_at=later,
                     instructions=[str(self.repo / "CLAUDE.md")])
        res, job = self.run_job("verify_active", cid)
        self.assertEqual(job["state"], "succeeded")
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["exposure"]), ("observed_active", "observed"))

    def test_loads_outside_the_repository_root_are_not_exposure(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        later = parse_iso(cand["applied_at"]) + timedelta(minutes=1)
        home = self.root / "home-user"
        for path in (home / ".claude" / "CLAUDE.md", self.repo / "src" / "CLAUDE.md"):
            with self.subTest(path=str(path)):
                self.episode(head=cand["applied_commit"], completed_at=later, instructions=[str(path)])
                res, job = self.run_job("verify_active", cid)
                self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "exposure_unknown"))
                self.assertEqual(self.candidate(cid)["status"], "applied")

    def test_user_level_skill_with_the_same_name_is_not_exposure(self):
        skill = ".claude/skills/review/SKILL.md"
        cand = self.applied({skill: "---\nname: review\ndescription: d\n---\nReview.\n"})
        cid = cand["candidate_id"]
        later = parse_iso(cand["applied_at"]) + timedelta(minutes=1)
        self.episode(head=cand["applied_commit"], completed_at=later,
                     skills=[str(self.root / "home-user" / ".claude" / "skills" / "review")])
        res, job = self.run_job("verify_active", cid)
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "exposure_unknown"))
        self.episode(head=cand["applied_commit"], completed_at=later,
                     skills=[str(self.repo / ".claude" / "skills" / "review")])
        res, job = self.run_job("verify_active", cid)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(self.candidate(cid)["status"], "observed_active")

    def test_unknown_exposure_is_not_credited_even_when_the_path_is_listed(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        later = parse_iso(cand["applied_at"]) + timedelta(minutes=1)
        self.episode(head=cand["applied_commit"], completed_at=later, instructions=[str(self.repo / "CLAUDE.md")],
                     exposure="unknown")
        res, job = self.run_job("verify_active", cid)
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "exposure_unknown"))
        self.assertEqual(self.candidate(cid)["status"], "applied")

    def test_episode_before_application_is_not_credited(self):
        cand = self.applied()
        earlier = parse_iso(cand["applied_at"]) - timedelta(minutes=1)
        self.episode(head=cand["applied_commit"], completed_at=earlier, instructions=[str(self.repo / "CLAUDE.md")])
        res, job = self.run_job("verify_active", cand["candidate_id"])
        self.assertEqual(job["state"], "retry_wait")
        self.assertEqual(self.candidate(cand["candidate_id"])["status"], "applied")

    def test_gives_up_after_fourteen_days_leaving_it_applied(self):
        cand = self.applied()
        clock = lambda: utc_now() + timedelta(days=15)  # noqa: E731
        res, job = self.run_job("verify_active", cand["candidate_id"], clock=clock)
        self.assertEqual(job["state"], "succeeded")
        row = self.candidate(cand["candidate_id"])
        self.assertEqual((row["status"], row["reason"], row["exposure"]), ("applied", "exposure_unobserved", "unknown"))


class NoteReviewTest(ApplyCase):
    def review_row(self, episode_id: str, *, signature="claims-tests-pass-when-failing", score=2,
                   affected=("CLAUDE.md",), status="valid") -> dict:
        dims = {d: {"score": 2, "reason": "r", "evidence_ids": ["e0001"]}
                for d in ("outcome", "instruction_adherence", "verification", "efficiency", "communication")}
        dims["verification"]["score"] = score
        review = {"dimensions": dims, "affected_paths": list(affected), "failure_signature": signature,
                  "improvements": [{"finding": "x", "basis": "observed", "evidence_ids": ["e0001"]}]}
        return {"review_id": new_id("rev"), "episode_id": episode_id, "status": status,
                "failure_signature": signature, "review_json": canonical_json(review)}

    def note(self, row) -> list:
        with store.transaction(self.conn):
            return apply.note_review(self.conn, row, utc_now())

    def test_same_signature_after_application_flags_regression_and_pauses(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        ep = self.episode(head=cand["applied_commit"], completed_at=utc_now())
        row = self.review_row(ep)
        self.assertEqual(self.note(row), [cid])
        self.assertEqual(self.candidate(cid)["status"], "regression_suspected")
        self.assertEqual(store.get_meta(self.conn, "apply_paused_reason"), f"regression_suspected:{cid}")
        jobs = self.jobs("reproduce_regression")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0]["payload_json"])["mode"], "reproduce")
        self.assertEqual(jobs[0]["candidate_id"], cid)

    def test_episode_before_the_applied_commit_is_ignored(self):
        cand = self.applied()
        ep = self.episode(head=self.base, completed_at=utc_now())
        self.assertEqual(self.note(self.review_row(ep)), [])
        self.assertEqual(self.candidate(cand["candidate_id"])["status"], "applied")
        self.assertIsNone(store.get_meta(self.conn, "apply_paused_reason"))

    def test_severe_failure_needs_a_link_to_the_change(self):
        cand = self.applied()
        ep = self.episode(head=cand["applied_commit"], completed_at=utc_now())
        self.assertEqual(self.note(self.review_row(ep, signature="other-thing", score=0, affected=["src/app.py"])),
                         [])
        self.assertEqual(self.candidate(cand["candidate_id"])["status"], "applied")
        self.assertEqual(self.note(self.review_row(ep, signature="other-thing", score=0)), [cand["candidate_id"]])
        self.assertEqual(self.candidate(cand["candidate_id"])["reason"], "severe_failure")

    def test_rejected_review_and_internal_errors_change_nothing(self):
        cand = self.applied()
        ep = self.episode(head=cand["applied_commit"], completed_at=utc_now())
        self.assertEqual(self.note(self.review_row(ep, status="rejected")), [])
        from unittest import mock

        with mock.patch.object(apply, "_set_meta_in", side_effect=RuntimeError("boom")):
            self.assertEqual(self.note(self.review_row(ep)), [])
        self.assertEqual(self.candidate(cand["candidate_id"])["status"], "applied")


class RevertTest(ApplyCase):
    def revert_now(self, cid: str):
        return self.run_job("reproduce_regression", cid, payload={"candidate_id": cid, "mode": "revert_now"},
                            dedup=f"reproduce_regression:{cid}:manual:{new_id('x')}")

    def test_clean_revert_is_applied_with_a_trailer(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        res, job = self.revert_now(cid)
        self.assertEqual(job["state"], "succeeded", job)
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), cand["applied_commit"])
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), BASE_CLAUDE_MD)
        self.assertIn(f"Autotune-Revert: {cid}", git(self.repo, "log", "-1", "--format=%B"))
        self.assertEqual(git(self.repo, "log", "-1", "--format=%an"), "Autotune")
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reverted_commit"]), ("reverted", head))
        self.assertIsNotNone(row["reverted_at"])
        self.assertIsNone(store.get_meta(self.conn, "apply_paused_reason"))
        self.assertEqual(git(self.repo, "branch", "--list", f"autotune/revert-{cid}"), "")
        self.assertFalse((self.data_dir / "worktrees" / f"revert-{cid}").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), "")

    def test_conflicting_revert_stops_safely(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        (self.repo / "CLAUDE.md").write_text(USEFUL_CLAUDE_MD.replace("as failing.", "as failing, always."))
        git(self.repo, "commit", "-q", "-am", "user edits the same line")
        before = snapshot(self.repo)
        res, job = self.revert_now(cid)
        self.assertEqual((job["state"], job["last_error"]), ("failed", "revert_conflict"))
        self.assertEqual(snapshot(self.repo), before)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("regression_suspected", "revert_conflict"))
        self.assertIsNone(row["reverted_commit"])
        self.assertEqual(git(self.repo, "branch", "--list", f"autotune/revert-{cid}"), "")
        self.assertFalse((self.data_dir / "worktrees" / f"revert-{cid}").exists())

    def test_revert_waits_for_a_clean_checkout(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        (self.repo / "src/app.py").write_text("print('mine')\n")
        before = snapshot(self.repo)
        res, job = self.revert_now(cid)
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "checkout_dirty"))
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.candidate(cid)["status"], "regression_suspected")
        # The prepared revert is reused once the checkout is clean.
        git(self.repo, "checkout", "-q", "--", "src/app.py")
        prepared = git(self.repo, "rev-parse", f"autotune/revert-{cid}")
        res, job = self.revert_now(cid)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared)

    def test_prepared_revert_is_reused_not_rebuilt(self):
        from unittest import mock

        cand = self.applied()
        cid = cand["candidate_id"]
        (self.repo / "src/app.py").write_text("print('mine')\n")
        with mock.patch.object(git_ops, "revert_in_worktree", wraps=git_ops.revert_in_worktree) as made:
            res, job = self.revert_now(cid)
            self.assertEqual(job["last_error"], "checkout_dirty")
            git(self.repo, "checkout", "-q", "--", "src/app.py")
            res, job = self.revert_now(cid)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(made.call_count, 1)

    def test_revert_waits_for_foreground_activity(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        store.record_activity(self.conn, "sess-now", "active", "t1", utc_now() - timedelta(minutes=5))
        before = snapshot(self.repo)
        res, job = self.revert_now(cid)
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "foreground_active"))
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.candidate(cid)["status"], "regression_suspected")

    def test_revert_after_the_user_reset_away_from_the_application_stops(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        git(self.repo, "reset", "-q", "--hard", self.base)
        before = snapshot(self.repo)
        res, job = self.revert_now(cid)
        self.assertEqual((job["state"], job["last_error"]), ("failed", "git_reconciliation_required"))
        self.assertEqual(snapshot(self.repo), before)
        self.assertIsNotNone(store.get_meta(self.conn, "git_reconciliation_required"))
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("regression_suspected", "git_reconciliation_required"))

    def test_head_moving_while_the_revert_is_prepared_waits(self):
        from unittest import mock

        cand = self.applied()
        cid = cand["candidate_id"]
        real = apply._checkout_problem

        def user_commits(repo, from_rev, to_rev):
            (self.repo / "src/app.py").write_text("print('user commit')\n")
            git(self.repo, "commit", "-q", "-am", "user commit during revert")
            return real(repo, from_rev, to_rev)

        with mock.patch.object(apply, "_checkout_problem", user_commits):
            res, job = self.revert_now(cid)
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "revert_head_moved"))
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "user commit during revert")
        self.assertEqual(git(self.repo, "branch", "--list", f"autotune/revert-{cid}"), "")
        self.assertEqual(self.candidate(cid)["status"], "regression_suspected")

    def test_revert_touching_other_paths_is_refused(self):
        from unittest import mock

        cand = self.applied()
        cid = cand["candidate_id"]
        real = git_ops.changed_files

        def changed(repo, base, rev):
            out = real(repo, base, rev)
            if base == cand["applied_commit"]:
                out = out + [git_ops.Change("src/app.py", "M", "100644", "100644", False)]
            return out

        before = snapshot(self.repo)
        with mock.patch.object(git_ops, "changed_files", changed):
            res, job = self.revert_now(cid)
        self.assertEqual((job["state"], job["last_error"]), ("failed", "revert_scope_violation"))
        self.assertEqual(snapshot(self.repo), before)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("regression_suspected", "revert_scope_violation"))
        self.assertEqual(git(self.repo, "branch", "--list", f"autotune/revert-{cid}"), "")


class ReproduceTest(ApplyCase):
    def flag(self, cand) -> None:
        cid = cand["candidate_id"]
        with store.transaction(self.conn):
            self.conn.execute("UPDATE candidates SET status = 'regression_suspected', reason = 'same_failure_signature' "
                              "WHERE candidate_id = ?", (cid,))
            apply._set_meta_in(self.conn, "apply_paused_reason", f"regression_suspected:{cid}")

    def reproduce(self, cid):
        return self.run_job("reproduce_regression", cid, payload={"candidate_id": cid, "mode": "reproduce"},
                            dedup=f"reproduce_regression:{cid}:r1")

    def test_not_reproduced_returns_to_applied_and_clears_the_pause(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        self.flag(cand)
        res, job = self.reproduce(cid)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertFalse(res.result["reproduced"])
        row = self.candidate(cid)
        self.assertEqual(row["status"], "applied")
        self.assertIsNone(store.get_meta(self.conn, "apply_paused_reason"))
        runs = self.runs(cid)
        self.assertEqual(len(runs), 6)
        self.assertEqual({r["harness_commit"] for r in runs if r["arm"] == "candidate"}, {cand["applied_commit"]})
        self.assertEqual({r["harness_commit"] for r in runs if r["arm"] == "baseline"}, {self.base})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), cand["applied_commit"])

    def test_reproduced_regression_is_reverted(self):
        cand = self.applied({"CLAUDE.md": NO_VERIFY_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.flag(cand)
        res, job = self.reproduce(cid)
        self.assertEqual(job["state"], "succeeded", job)
        row = self.candidate(cid)
        self.assertEqual(row["status"], "reverted")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), cand["applied_commit"])
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), BASE_CLAUDE_MD)
        self.assertEqual(res.result["reproduction"]["checks"]["critical"], "fail")

    def test_stochastic_regression_counts_as_reproduced(self):
        from types import SimpleNamespace

        checks = {name: "pass" for name in ("integrity", "static", "critical", "known_successes", "target_effect",
                                            "judgment", "cost", "completeness")}
        checks["known_successes"] = "fail"
        decision = {"decision": "inconclusive", "reason_codes": ["stochastic_regression"], "checks": checks}
        exp = SimpleNamespace(candidate_id="cand_none", cases=[], first_attempt=1)
        self.assertIs(apply._reproduced(decision, exp, self.conn), True)

    def test_reproduction_attempts_continue_after_earlier_runs(self):
        cand = self.applied()
        cid = cand["candidate_id"]
        self.flag(cand)
        self.reproduce(cid)
        self.flag(cand)
        self.run_job("reproduce_regression", cid, payload={"candidate_id": cid, "mode": "reproduce"},
                     dedup=f"reproduce_regression:{cid}:r2")
        attempts = sorted({r["attempt"] for r in self.runs(cid)})
        self.assertEqual(attempts, [1, 2])


class GitOpsApplyTest(Cycle2Case):
    def test_status_entries_distinguish_untracked_from_tracked_changes(self):
        (self.repo / "src/app.py").write_text("x\n")
        (self.repo / "new.txt").write_text("y\n")
        entries = {(e.xy, e.path) for e in git_ops.status_entries(self.repo)}
        self.assertEqual(entries, {(" M", "src/app.py"), ("??", "new.txt")})
        self.assertEqual(git_ops.tracked_changes(self.repo), ["src/app.py"])

    def test_merge_ff_only_refuses_a_diverged_commit_and_changes_nothing(self):
        git(self.repo, "switch", "-q", "-c", "side")
        (self.repo / "src/app.py").write_text("side\n")
        git(self.repo, "commit", "-q", "-am", "side")
        side = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "CLAUDE.md").write_text("main\n")
        git(self.repo, "commit", "-q", "-am", "main")
        before = snapshot(self.repo)
        with self.assertRaises(git_ops.GitError):
            git_ops.merge_ff_only(self.repo, side)
        self.assertEqual(snapshot(self.repo), before)

    def test_merge_ff_only_runs_no_hooks(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "hook-ran"
        for name in ("post-merge", "reference-transaction", "post-checkout"):
            (hooks / name).write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n")
            (hooks / name).chmod(0o755)
        git(self.repo, "config", "core.hooksPath", str(hooks))
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        marker.unlink(missing_ok=True)
        self.assertEqual(git_ops.merge_ff_only(self.repo, cand["candidate_commit"]), cand["candidate_commit"])
        self.assertFalse(marker.exists())
        self.assertTrue(git_ops.worktree_matches(self.repo, cand["candidate_commit"]))

    def test_merge_ff_only_never_autostashes_local_changes(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        git(self.repo, "config", "merge.autoStash", "true")
        (self.repo / "CLAUDE.md").write_text("my edit\n")
        before = snapshot(self.repo)
        with self.assertRaises(git_ops.GitError):
            git_ops.merge_ff_only(self.repo, cand["candidate_commit"])
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(git(self.repo, "stash", "list"), "")

    def test_trailer_search_by_key(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.assertEqual(git_ops.find_trailer_commits_for(self.repo, git_ops.TRAILER_KEY, cid),
                         [cand["candidate_commit"]])
        self.assertEqual(git_ops.find_trailer_commits_for(self.repo, git_ops.REVERT_TRAILER_KEY, cid), [])
        self.assertEqual(git_ops.find_trailer_commits_for(self.repo, git_ops.TRAILER_KEY, cid,
                                                          refs=["refs/heads/main"]), [])
        self.assertTrue(git_ops.is_ancestor_or_equal(self.repo, self.base, cand["candidate_commit"]))
        self.assertFalse(git_ops.is_ancestor_or_equal(self.repo, cand["candidate_commit"], self.base))
        self.assertFalse(git_ops.is_ancestor_or_equal(self.repo, "0" * 40, self.base))


if __name__ == "__main__":
    import unittest

    unittest.main()
