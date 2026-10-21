"""Cycle 3 commands: consolidate, calibrate, mutations, export, uninstall, and
the retention of candidate branches. Real temporary Git repos; no model is
called (calibrate and mutations run against stub modules)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import types
from datetime import timedelta
from pathlib import Path
from unittest import mock

import autotune
from autotune import cli, export, git_ops, ingress, paths, queue, retention, review, store
from autotune.util import canonical_json, iso, sha256_text, utc_now

from tests._cycle2_support import git, snapshot
from tests._runtime_support import RuntimeCase

_MISSING = object()


class Cycle3CliCase(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.dd = paths.project_data_dir(self.common)
        self.conn = self.db()
        self.base = git(self.repo, "rev-parse", "HEAD")

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--project-dir", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def use_module(self, name: str, module) -> None:
        p = mock.patch.dict(sys.modules, {f"autotune.{name}": module})
        p.start()
        self.addCleanup(p.stop)
        saved = autotune.__dict__.get(name, _MISSING)
        if module is None:
            autotune.__dict__.pop(name, None)
        else:
            setattr(autotune, name, module)

        def restore():
            if saved is _MISSING:
                autotune.__dict__.pop(name, None)
            else:
                setattr(autotune, name, saved)
        self.addCleanup(restore)

    def candidate(self, cid: str, status: str, *, days_old: float = 0, branch=_MISSING, **cols) -> str:
        ts = iso(utc_now() - timedelta(days=days_old))
        row = {
            "candidate_id": cid, "hypothesis": "h", "effect_metric": "m", "allowed_paths_json": '["CLAUDE.md"]',
            "change_kind": "instruction", "branch": f"autotune/{cid}" if branch is _MISSING else branch,
            "base_commit": self.base, "status": status, "created_at": ts, "updated_at": ts, **cols,
        }
        with store.transaction(self.conn):
            self.conn.execute(f"INSERT INTO candidates ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                              list(row.values()))
        return cid

    def autotune_branch(self, cid: str, *, worktree: bool = False) -> str:
        """An autotune/<cid> branch with one commit made by Autotune; the
        worktree is kept under the data dir when asked."""
        wt = self.dd / "worktrees" / cid
        git_ops.create_worktree(self.repo, f"autotune/{cid}", self.base, wt)
        (wt / "CLAUDE.md").write_text(f"rule for {cid}\n")
        git(wt, "add", "CLAUDE.md")
        commit = git_ops.commit_candidate(wt, "candidate", cid, repo=self.repo)
        if not worktree:
            git_ops.remove_worktree(self.repo, wt, delete_branch=None, worktrees_root=self.dd / "worktrees")
        return commit

    def branches(self) -> set[str]:
        return set(git(self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


class ConsolidateCommandTests(Cycle3CliCase):
    def test_enqueues_one_sweep_and_requeues_it_after_it_finishes(self):
        code, out, _ = self.cli("consolidate")
        self.assertEqual(code, 0, out)
        self.assertIn("Queued consolidation sweep job", out)
        [job] = self.conn.execute("SELECT * FROM jobs WHERE job_type = 'consolidate'").fetchall()
        self.assertEqual(job["dedup_key"], review.SWEEP_DEDUP_KEY)
        self.assertEqual(json.loads(job["payload_json"])["mode"], "sweep")
        self.assertIsNotNone(store.get_meta(self.conn, review.SWEEP_META))

        code, out, _ = self.cli("consolidate")
        self.assertIn("Already pending:", out)
        with store.transaction(self.conn):
            self.conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = ?", (job["id"],))
        code, out, _ = self.cli("consolidate")
        self.assertIn("Requeued", out)
        [again] = self.conn.execute("SELECT * FROM jobs WHERE job_type = 'consolidate'").fetchall()
        self.assertEqual((again["id"], again["state"]), (job["id"], "queued"))

    def test_requires_enrollment(self):
        (self.repo / ".autotune.json").unlink()
        code, _, err = self.cli("consolidate")
        self.assertEqual(code, 1)
        self.assertIn("not enrolled", err)


class ToolingCommandTests(Cycle3CliCase):
    def stub(self, name: str, ret=0):
        calls = []
        module = types.ModuleType(f"autotune.{name}")
        module.main_args = lambda args: calls.append(args) or ret
        self.use_module(name, module)
        return calls

    def test_calibrate_without_live_runs_nothing_and_exits_2(self):
        calls = self.stub("calibrate")
        code, out, _ = self.cli("calibrate", "--set", "calibration/seed")
        self.assertEqual(code, 2)
        self.assertIn("--live", out)
        self.assertEqual(calls, [])

    def test_calibrate_live_passes_its_arguments(self):
        calls = self.stub("calibrate", ret=3)
        code, _, _ = self.cli("calibrate", "--set", "seed", "--out", "r.json", "--live")
        self.assertEqual(code, 3)
        [args] = calls
        self.assertEqual((args.set_dir, args.out, args.live, args.project_dir), ("seed", "r.json", True,
                                                                                  str(self.repo)))

    def test_missing_tooling_module_fails_cleanly(self):
        self.use_module("mutations", None)
        code, _, err = self.cli("mutations")
        self.assertEqual(code, 1)
        self.assertIn("mutations module is unavailable", err)

    def test_mutations_simulated_by_default_and_live_needs_a_budget(self):
        calls = self.stub("mutations")
        code, _, _ = self.cli("mutations", "--set", "benchmarks/mutations")
        self.assertEqual(code, 0)
        self.assertEqual((calls[0].set_dir, calls[0].live, calls[0].budget_calls), ("benchmarks/mutations", False,
                                                                                    None))
        code, out, _ = self.cli("mutations", "--live")
        self.assertEqual(code, 2)
        self.assertIn("--budget-calls", out)
        self.assertEqual(len(calls), 1)
        code, _, _ = self.cli("mutations", "--live", "--budget-calls", "0")
        self.assertEqual(code, 2)
        code, _, _ = self.cli("mutations", "--live", "--budget-calls", "40")
        self.assertEqual((code, calls[-1].budget_calls), (0, 40))


class ExportTests(Cycle3CliCase):
    SESSION = "8f14e45f-ceea-467a-9575-0123456789ab"

    def episode(self) -> str:
        top = str(self.repo)
        repo_json = {"toplevel": top, "git_common_dir": self.common, "branch": "main", "head": self.base,
                     "tracked_dirty": False, "errors": []}
        row = {
            "episode_id": "ep_export1", "session_id": self.SESSION, "turn_id": "t1", "lifecycle_id": "lc-77",
            "lifecycle_source": "startup", "origin": "foreground", "task_id": None,
            "started_at": "2026-03-04T11:59:00.000Z", "completed_at": "2026-03-04T12:00:00.000Z",
            "captured_at": iso(utc_now()), "end_reason": "answer", "duration_ms": 1000, "model": "m",
            "transcript_path": f"/Users/someone/.claude/projects/x/{self.SESSION}.jsonl",
            "repository_json": canonical_json(repo_json),
            "loaded_harness_json": canonical_json({"instructions": [{"file_path": f"{top}/CLAUDE.md"}],
                                                   "skills": [], "exposure": "observed"}),
            "coverage_json": canonical_json({}), "usage_json": None,
        }
        contents = [
            ("user_request", "user", f"Fix {top}/src/app.py, see /Users/someone/notes.txt", {"source": "x"}),
            ("tool_call", "assistant", json.dumps({"command": f"cat {top}/README.md token=abcd1234efgh"}),
             {"tool": "Bash", "tool_use_id": "toolu_secret"}),
            ("assistant_answer", "assistant", f"Done in session {self.SESSION}. Docs: https://example.com/a/b",
             {"source": "y"}),
        ]
        rows = [{"evidence_id": f"e{i:04d}", "position": i, "kind": k, "role": r, "content": c,
                 "content_sha256": sha256_text(c), "provenance_json": canonical_json(p), "truncated": 0}
                for i, (k, r, c, p) in enumerate(contents, start=1)]
        store.ingest_turn_complete(self.conn, row, rows, utc_now())
        return row["episode_id"]

    def test_export_writes_a_sanitized_fixture_and_reports_removals(self):
        ep = self.episode()
        out_path = self.root / "fixture.json"
        code, out, _ = self.cli("export", ep, "--out", str(out_path))
        self.assertEqual(code, 0, out)
        text = out_path.read_text()
        for leaked in (str(self.repo), "/Users/someone", self.SESSION, "abcd1234efgh", "lc-77", self.common,
                       "transcript"):
            self.assertNotIn(leaked, text)
        data = json.loads(text)
        self.assertEqual(oct(out_path.stat().st_mode & 0o777), "0o600")
        self.assertIs(data["synthetic"], False)
        ev = {e["id"]: e for e in data["evidence"]}
        self.assertEqual(ev["e0001"]["content"], "Fix src/app.py, see <redacted-path>")
        self.assertIn("cat README.md", ev["e0002"]["content"])
        self.assertNotIn("tool_use_id", ev["e0002"]["provenance"])
        self.assertIn("https://example.com/a/b", ev["e0003"]["content"])
        self.assertIn(export.hash_id(self.SESSION), ev["e0003"]["content"])
        self.assertEqual(data["episode"]["repository"], {"branch": "main", "head": self.base,
                                                         "tracked_dirty": False})
        self.assertEqual(data["episode"]["loaded_harness"]["instructions"], [{"file_path": "CLAUDE.md"}])
        self.assertIn("1 secret-shaped strings redacted", out)
        self.assertIn("episode.transcript_path", out)
        self.assertIn("episode.repository.toplevel", out)

    def test_refuses_expired_evidence_and_existing_output(self):
        ep = self.episode()
        existing = self.root / "exists.json"
        existing.write_text("keep me")
        code, out, _ = self.cli("export", ep, "--out", str(existing))
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        self.assertEqual(existing.read_text(), "keep me")

        with store.transaction(self.conn):
            self.conn.execute("UPDATE evidence SET content = NULL, expired_at = ? WHERE evidence_id = 'e0002'",
                              (iso(utc_now()),))
        target = self.root / "new.json"
        code, out, _ = self.cli("export", ep, "--out", str(target))
        self.assertEqual(code, 1)
        self.assertIn("e0002", out)
        self.assertFalse(target.exists())

    def test_refuses_evidence_past_retention_before_the_worker_clears_it(self):
        ep = self.episode()
        # retain_until has passed but no retention pass has run (expired_at is NULL).
        with store.transaction(self.conn):
            self.conn.execute("UPDATE evidence SET retain_until = ? WHERE evidence_id = 'e0003'",
                              ("2020-01-01T00:00:00.000Z",))
        target = self.root / "late.json"
        code, out, _ = self.cli("export", ep, "--out", str(target))
        self.assertEqual(code, 1, out)
        self.assertIn("e0003", out)
        self.assertFalse(target.exists())
        # The episode is older than raw_retention_days (30 by default), retain_until still ahead.
        with store.transaction(self.conn):
            self.conn.execute("UPDATE evidence SET retain_until = ?", (iso(utc_now() + timedelta(days=90)),))
            self.conn.execute("UPDATE episodes SET captured_at = ? WHERE episode_id = ?",
                              (iso(utc_now() - timedelta(days=31)), ep))
        code, out, _ = self.cli("export", ep, "--out", str(target))
        self.assertEqual(code, 1, out)
        self.assertIn("has expired", out)
        self.assertFalse(target.exists())
        self.assertIsNone(self.conn.execute("SELECT expired_at FROM evidence WHERE evidence_id = 'e0001'")
                          .fetchone()[0])

    def test_paths_after_a_colon_are_redacted(self):
        clean = export.Sanitizer(["/Users/tim/proj"], [])
        self.assertEqual(clean.text("PATH=/usr/bin:/Users/tim/.local/bin"), "PATH=<redacted-path>:<redacted-path>")
        self.assertEqual(clean.text("x.py:/home/tim/a"), "x.py:<redacted-path>")
        self.assertEqual(clean.text("see file:/Users/tim/secret.txt"), "see file:<redacted-path>")
        self.assertEqual(clean.text("CLASSPATH=/opt/lib/a.jar:/home/tim/b.jar"),
                         "CLASSPATH=<redacted-path>:<redacted-path>")
        self.assertEqual(clean.text("/Users/tim/proj/src/app.py:12: error"), "src/app.py:12: error")
        for url in ("https://example.com/a/b", "http://x/y", "ssh://git@host/repo.git"):
            self.assertEqual(clean.text(url), url)

    def test_unknown_episode(self):
        code, out, _ = self.cli("export", "ep_nope", "--out", str(self.root / "x.json"))
        self.assertEqual(code, 1)
        self.assertIn("no episode ep_nope", out)


class CandidateBranchRetentionTests(Cycle3CliCase):
    def test_old_rejected_and_inconclusive_branches_go_and_everything_else_stays(self):
        old = 181
        self.autotune_branch("cand_oldrej")
        self.candidate("cand_oldrej", "rejected", days_old=old)
        git(self.repo, "branch", "autotune/cand_oldinc", self.base)
        self.candidate("cand_oldinc", "inconclusive", days_old=old)
        self.autotune_branch("cand_young")
        self.candidate("cand_young", "rejected", days_old=10)
        applied_commit = self.autotune_branch("cand_applied")
        self.candidate("cand_applied", "applied", days_old=old, applied_commit=applied_commit)
        # The user committed on top of this one: never deleted.
        self.autotune_branch("cand_foreign")
        git(self.repo, "checkout", "-q", "autotune/cand_foreign")
        (self.repo / "mine.txt").write_text("mine\n")
        git(self.repo, "add", "mine.txt")
        git(self.repo, "commit", "-q", "-m", "my work")
        git(self.repo, "checkout", "-q", "main")
        self.candidate("cand_foreign", "rejected", days_old=old)
        self.candidate("cand_gone", "rejected", days_old=old)  # branch already deleted

        counts = retention.apply_retention(self.conn, utc_now(), 30, 180, repo=str(self.repo))
        self.assertEqual((counts["branches_deleted"], counts["branches_kept"]), (2, 1))
        left = self.branches()
        self.assertNotIn("autotune/cand_oldrej", left)
        self.assertNotIn("autotune/cand_oldinc", left)
        for kept in ("autotune/cand_young", "autotune/cand_applied", "autotune/cand_foreign", "main"):
            self.assertIn(kept, left)
        deleted_at = {r["candidate_id"]: r["branch_deleted_at"] for r in
                      self.conn.execute("SELECT candidate_id, branch_deleted_at FROM candidates")}
        self.assertIsNotNone(deleted_at["cand_oldrej"])
        self.assertIsNotNone(deleted_at["cand_gone"])
        self.assertIsNone(deleted_at["cand_young"])
        self.assertIsNone(deleted_at["cand_applied"])
        self.assertIsNone(deleted_at["cand_foreign"])
        # Decision records stay.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 6)

    def test_a_leftover_candidate_worktree_is_removed_with_its_branch(self):
        self.autotune_branch("cand_wt", worktree=True)
        wt = self.dd / "worktrees" / "cand_wt"
        self.candidate("cand_wt", "inconclusive", days_old=200, worktree_path=str(wt))
        counts = retention.apply_retention(self.conn, utc_now(), 30, 180, repo=str(self.repo))
        self.assertEqual(counts["branches_deleted"], 1)
        self.assertFalse(wt.exists())
        self.assertNotIn("autotune/cand_wt", self.branches())

    def test_repository_found_from_project_json(self):
        self.autotune_branch("cand_oldrej")
        self.candidate("cand_oldrej", "rejected", days_old=200)
        retention.apply_retention(self.conn, utc_now(), 30, 180)
        self.assertIn("autotune/cand_oldrej", self.branches())  # no project.json yet: skipped
        ingress.write_project_meta(self.dd, ingress.git_facts(self.repo))
        counts = retention.apply_retention(self.conn, utc_now(), 30, 180)
        self.assertEqual(counts["branches_deleted"], 1)
        self.assertNotIn("autotune/cand_oldrej", self.branches())


class UninstallTests(Cycle3CliCase):
    def setUp(self) -> None:
        super().setUp()
        repo = self.repo
        git(repo, "branch", "feature")
        git(repo, "branch", "autotune/mine")  # the user's, though inside the prefix
        self.user_wt = self.root / "user-wt"
        git(repo, "worktree", "add", "-q", "-b", "feature2", str(self.user_wt))
        (self.user_wt / "wip.txt").write_text("user work in progress\n")
        (repo / "untracked.txt").write_text("not committed\n")
        (repo / "notes").mkdir()
        (repo / "notes" / "todo.md").write_text("todo\n")

        self.autotune_branch("cand_a", worktree=True)
        self.candidate("cand_a", "evaluating", worktree_path=str(self.dd / "worktrees" / "cand_a"))
        self.autotune_branch("cand_b")
        self.candidate("cand_b", "rejected")
        git(repo, "branch", "autotune/revert-cand_b", self.base)
        self.autotune_branch("cand_c")
        git(repo, "checkout", "-q", "autotune/cand_c")
        (repo / "user-edit.txt").write_text("user commit\n")
        git(repo, "add", "user-edit.txt")
        git(repo, "commit", "-q", "-m", "user commit on an autotune branch")
        git(repo, "checkout", "-q", "main")
        (repo / "untracked.txt").write_text("not committed\n")
        self.candidate("cand_c", "rejected")
        (self.dd / "logs").mkdir(exist_ok=True)
        (self.dd / "logs" / "autotune.log").write_text("{}\n")

    def user_state(self) -> dict:
        return {"main": snapshot(self.repo), "user_wt": snapshot_worktree(self.user_wt)}

    def test_without_yes_lists_the_plan_and_removes_nothing(self):
        before = (self.branches(), snapshot(self.repo))
        code, out, _ = self.cli("uninstall")
        self.assertEqual(code, 1)
        self.assertIn("Re-run with --yes", out)
        self.assertIn("branch autotune/cand_a", out)
        self.assertIn("keeps branch autotune/cand_c", out)
        self.assertNotIn("autotune/mine", out)
        self.assertEqual((self.branches(), snapshot(self.repo)), before)
        self.assertTrue(self.dd.exists())

    def test_removes_only_what_autotune_created(self):
        before = self.user_state()
        branches_before = self.branches()
        code, out, _ = self.cli("uninstall", "--yes")
        self.assertEqual(code, 0, out)

        self.assertFalse(self.dd.exists())
        self.assertTrue(paths.base_dir().exists())
        gone = {"autotune/cand_a", "autotune/cand_b", "autotune/revert-cand_b"}
        self.assertEqual(self.branches(), branches_before - gone)
        self.assertIn("autotune/mine", self.branches())
        self.assertIn("autotune/cand_c", self.branches())
        self.assertEqual(self.user_state(), before)
        listed = [Path(p) for p in git_ops.worktree_paths(self.repo)]
        self.assertEqual([os.path.realpath(p) for p in listed],
                         [os.path.realpath(self.repo), os.path.realpath(self.user_wt)])
        self.assertTrue((self.repo / ".autotune.json").exists())
        self.assertEqual((self.user_wt / "wip.txt").read_text(), "user work in progress\n")
        self.assertIn("branch autotune/cand_c", out.split("Kept:")[1])
        self.assertIn(".autotune.json is still present", out)

        code, out, _ = self.cli("uninstall", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to remove", out)
        self.assertEqual(self.user_state(), before)

    def worker_holder(self, ignore_term: bool) -> subprocess.Popen:
        lock = paths.worker_lock_path(self.dd)
        code = textwrap.dedent(f"""
            import signal, sys, time
            sys.path.insert(0, {str(Path(cli.__file__).resolve().parent.parent)!r})
            from autotune import locks
            if {ignore_term!r}:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            lock = locks.FileLock({str(lock)!r})
            assert lock.acquire()
            print("held", flush=True)
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_stops_a_running_worker_first(self):
        proc = self.worker_holder(ignore_term=False)
        with mock.patch.object(cli, "WORKER_STOP_SECONDS", 10.0):
            code, out, _ = self.cli("uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(self.dd.exists())

    def test_a_worker_that_does_not_stop_leaves_everything_in_place(self):
        self.worker_holder(ignore_term=True)
        branches = self.branches()
        self.assertIsNone(store.get_meta(self.conn, "paused"))
        with mock.patch.object(cli, "WORKER_STOP_SECONDS", 0.5):
            code, out, _ = self.cli("uninstall", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("nothing was removed", out)
        self.assertTrue(paths.db_path(self.dd).exists())
        self.assertEqual(self.branches(), branches)
        # Not left paused by the failed attempt.
        self.assertIsNone(store.get_meta(self.conn, "paused"))
        store.set_meta(self.conn, "paused", "0")
        with mock.patch.object(cli, "WORKER_STOP_SECONDS", 0.5):
            self.assertEqual(self.cli("uninstall", "--yes")[0], 1)
        self.assertEqual(store.get_meta(self.conn, "paused"), "0")

    def test_the_worker_is_not_signalled_during_an_application(self):
        """A worker in the middle of an apply or revert holds the apply lock:
        uninstall waits for it before SIGTERM, so a fast-forward of the
        user's checkout is never interrupted."""
        from autotune import apply

        worker_lock = paths.worker_lock_path(self.dd)
        apply_lock = apply.apply_lock_path(self.dd)
        code = textwrap.dedent(f"""
            import signal, sys, time
            sys.path.insert(0, {str(Path(cli.__file__).resolve().parent.parent)!r})
            from autotune import locks
            state = {{"applying": True}}
            signal.signal(signal.SIGTERM, lambda *a: sys.exit(0 if not state["applying"] else 3))
            worker = locks.FileLock({str(worker_lock)!r})
            busy = locks.FileLock({str(apply_lock)!r})
            assert worker.acquire() and busy.acquire()
            print("held", flush=True)
            time.sleep(1.5)
            state["applying"] = False
            busy.release()
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "held")
        with mock.patch.object(cli, "WORKER_STOP_SECONDS", 10.0):
            code, out, _ = self.cli("uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(proc.wait(timeout=10), 0, "SIGTERM arrived while the apply lock was held")
        self.assertFalse(self.dd.exists())

    def test_branches_made_after_the_plan_are_removed_too(self):
        real = cli._uninstall_plan
        calls = []

        def plan_then_worker_adds_a_branch(project):
            out = real(project)
            if not calls:
                self.autotune_branch("cand_late")
                self.candidate("cand_late", "rejected")
            calls.append(out)
            return out

        with mock.patch.object(cli, "_uninstall_plan", side_effect=plan_then_worker_adds_a_branch):
            code, out, _ = self.cli("uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("autotune/cand_late", [b for b, _ in calls[0]["branches"]])
        self.assertNotIn("autotune/cand_late", self.branches())
        self.assertIn("Also found since the plan was listed: branch autotune/cand_late", out)
        self.assertIn("branch autotune/cand_late", out.split("Removed:")[1])


def snapshot_worktree(wt: Path) -> dict:
    files = {}
    for dirpath, _, names in os.walk(wt):
        for name in names:
            full = Path(dirpath) / name
            if full.name == ".git":
                continue
            files[full.relative_to(wt).as_posix()] = full.read_bytes()
    return {"files": files, "head": git(wt, "rev-parse", "HEAD"),
            "status": git(wt, "status", "--porcelain=v1", "--untracked-files=all")}


if __name__ == "__main__":
    import unittest

    unittest.main()
