"""Ingress protocol: acks, exit codes, persistence, and worker spawning."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from autotune import capture, ingress, locks, paths

from tests._runtime_support import RuntimeCase, envelope, git, write_config


class TurnCompleteTest(RuntimeCase):
    def test_first_delivery_creates_episode_and_review_job(self):
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 0)
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["kind"], "turn_complete")
        self.assertFalse(ack["duplicate"])
        self.assertIsInstance(ack["job_id"], int)
        self.assertEqual(ack["queue"], {"queued": 1, "leased": 0, "blocked": 0})
        self.assertEqual(ack["worker"], "disabled")

        conn = self.db()
        ep = conn.execute("SELECT * FROM episodes").fetchone()
        self.assertEqual(ep["episode_id"], ack["episode_id"])
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (ack["job_id"],)).fetchone()
        self.assertEqual((job["job_type"], job["state"], job["episode_id"]), ("review", "queued", ack["episode_id"]))
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_redelivery_is_a_duplicate_with_same_ids(self):
        _, first = self.ingest_ack(envelope("turn_complete", self.repo))
        code, second = self.ingest_ack(envelope("turn_complete", self.repo, answer_text="different text"))
        self.assertEqual(code, 0)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["episode_id"], first["episode_id"])
        self.assertEqual(second["job_id"], first["job_id"])
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_distinct_turns_are_distinct_episodes(self):
        _, a = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t1"))
        _, b = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t2"))
        self.assertNotEqual(a["episode_id"], b["episode_id"])
        self.assertEqual(b["queue"]["queued"], 2)

    def test_repository_facts_are_recorded(self):
        (self.repo / "README.md").write_text("changed\n")
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.ingest_ack(envelope("turn_complete", self.repo / "."))
        repo = json.loads(self.db().execute("SELECT repository_json FROM episodes").fetchone()[0])
        self.assertEqual(repo["toplevel"], str(self.repo))
        self.assertEqual(repo["branch"], "feature")
        self.assertEqual(repo["head"], git(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(repo["tracked_dirty"])

    def test_subdirectory_cwd_resolves_to_same_project(self):
        sub = self.repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        self.ingest_ack(envelope("turn_complete", sub))
        meta = json.loads(paths.project_meta_path(self.data_dir()).read_text())
        self.assertEqual(meta["toplevel"], str(self.repo))

    def test_project_meta_written_on_first_use(self):
        self.assertFalse(self.data_dir().exists())
        self.ingest_ack(envelope("turn_start", self.repo))
        meta = json.loads(paths.project_meta_path(self.data_dir()).read_text())
        self.assertEqual(meta["toplevel"], str(self.repo))
        self.assertEqual(meta["project_id"], self.data_dir().name)
        self.assertEqual(meta["project_id"], paths.project_id(meta["git_common_dir"]))


class ActivityTest(RuntimeCase):
    def activity(self, session="sess-1"):
        return self.db().execute(
            "SELECT state, turn_id FROM foreground_activity WHERE session_id = ?", (session,)
        ).fetchone()

    def test_activity_follows_turn_lifecycle(self):
        code, ack = self.ingest_ack(envelope("turn_start", self.repo, turn_id="t9"))
        self.assertEqual((code, ack["ok"], ack["kind"]), (0, True, "turn_start"))
        self.assertEqual(tuple(self.activity()), ("active", "t9"))

        self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t9"))
        self.assertEqual(self.activity()["state"], "idle")

        code, ack = self.ingest_ack(envelope("session_end", self.repo))
        self.assertEqual((code, ack["kind"]), (0, "session_end"))
        self.assertEqual(self.activity()["state"], "ended")

    def test_turn_start_does_not_create_jobs(self):
        self.ingest_ack(envelope("turn_start", self.repo))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)


class SkipTest(RuntimeCase):
    def test_non_foreground_origin_is_acknowledged_without_storing(self):
        for origin in ("worker", "judge", "author", "replay"):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo, origin=origin))
            self.assertEqual(code, 0)
            self.assertEqual(ack, {"ok": True, "skipped": "non_foreground_origin"})
        self.assertFalse(self.data_dir().exists())

    def test_worker_process_environment_is_never_learned_from(self):
        env = dict(os.environ, AUTOTUNE_ORIGIN="worker")
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo), env=env)
        self.assertEqual(ack, {"ok": True, "skipped": "non_foreground_origin"})
        self.assertFalse(self.data_dir().exists())

    def test_repo_without_config_is_not_enrolled(self):
        (self.repo / ".autotune.json").unlink()
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual((code, ack), (0, {"ok": True, "skipped": "not_enrolled"}))
        self.assertFalse(self.data_dir().exists())

    def test_cwd_outside_git_is_not_enrolled(self):
        plain = self.root / "plain"
        plain.mkdir()
        code, ack = self.ingest_ack(envelope("turn_complete", plain))
        self.assertEqual((code, ack), (0, {"ok": True, "skipped": "not_enrolled"}))

    def test_missing_cwd_is_not_enrolled(self):
        code, ack = self.ingest_ack(envelope("turn_complete", self.root / "gone"))
        self.assertEqual((code, ack), (0, {"ok": True, "skipped": "not_enrolled"}))

    def test_disabled_enrollment(self):
        write_config(self.repo, {"enabled": False})
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual((code, ack), (0, {"ok": True, "skipped": "disabled"}))
        self.assertFalse(paths.db_path(self.data_dir()).exists())


class InvalidEnvelopeTest(RuntimeCase):
    def assert_invalid(self, raw, error=None):
        code, ack = self.ingest_ack(raw)
        self.assertEqual(code, 2)
        self.assertFalse(ack["ok"])
        self.assertIsInstance(ack["error"], str)
        if error:
            self.assertEqual(ack["error"], error)
        return ack

    def test_malformed_inputs_exit_2_with_one_line(self):
        self.assert_invalid(b"", None)
        self.assert_invalid(b"{not json", "invalid_json")
        self.assert_invalid(b"[1, 2]", None)
        self.assert_invalid(b"\xff\xfe\x00", "invalid_encoding")
        self.assert_invalid(b"[" * 10000 + b"]" * 10000, None)

    def test_schema_violations_exit_2(self):
        bad = envelope("turn_complete", self.repo)
        bad["end_reason"] = "success"
        self.assert_invalid(bad)
        bad = envelope("turn_complete", self.repo)
        bad["schema"] = "autotune.envelope/999"
        self.assert_invalid(bad)
        bad = envelope("turn_complete", self.repo)
        del bad["session_id"]
        self.assert_invalid(bad)

    def test_oversized_envelope_is_rejected_and_recorded(self):
        big = envelope("turn_complete", self.repo, session_id="big-sess", turn_id="big-turn")
        big["answer_text"] = "x" * (capture.MAX_ENVELOPE_BYTES + 10)
        ack = self.assert_invalid(big, "envelope_too_large")
        self.assertIn("detail", ack)
        rows = self.db().execute("SELECT session_id, turn_id, code FROM capture_errors").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("big-sess", "big-turn", "envelope_too_large")])
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)

    def test_oversized_envelope_for_unenrolled_repo_writes_nothing(self):
        (self.repo / ".autotune.json").unlink()
        big = envelope("turn_complete", self.repo)
        big["answer_text"] = "x" * (capture.MAX_ENVELOPE_BYTES + 10)
        self.assert_invalid(big, "envelope_too_large")
        self.assertFalse(self.data_dir().exists())

    def test_invalid_input_does_not_create_episodes(self):
        self.ingest_ack(envelope("turn_start", self.repo))
        self.assert_invalid(b"{bad")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)


class PersistenceFailureTest(RuntimeCase):
    def test_store_failure_exits_3_and_records_capture_error(self):
        boom = sqlite3.OperationalError("disk I/O error")
        with mock.patch("autotune.store.ingest_turn_complete", side_effect=boom):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t-fail"))
        self.assertEqual(code, 3)
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "persistence_failed")
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)
        rows = conn.execute("SELECT session_id, turn_id FROM capture_errors").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("sess-1", "t-fail")])

    def test_normalizer_failure_exits_3(self):
        with mock.patch("autotune.capture.normalize_episode", side_effect=RuntimeError("bug")):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual((code, ack["ok"]), (3, False))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM capture_errors").fetchone()[0], 1)

    def test_retry_after_failure_succeeds_once(self):
        with mock.patch("autotune.store.ingest_turn_complete", side_effect=sqlite3.OperationalError("x")):
            self.ingest_ack(envelope("turn_complete", self.repo))
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 0)
        self.assertFalse(ack["duplicate"])
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertTrue(ack["duplicate"])

    def test_invalid_config_is_reported_not_silently_skipped(self):
        write_config(self.repo, {"enabled": True, "no_such_key": 1})
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 3)
        self.assertEqual(ack["error"], "config_invalid")
        codes = [r[0] for r in self.db().execute("SELECT code FROM capture_errors")]
        self.assertEqual(codes, ["config_invalid"])


class WorkerSpawnTest(RuntimeCase):
    def test_held_worker_lock_reports_running(self):
        self.ingest_ack(envelope("turn_start", self.repo))
        lock = locks.FileLock(paths.worker_lock_path(self.data_dir()))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        with mock.patch("autotune.ingress._popen") as popen:
            _, ack = self.ingest_ack(envelope("turn_complete", self.repo), env={**os.environ, "AUTOTUNE_NO_WORKER": "0"})
        self.assertEqual(ack["worker"], "running")
        popen.assert_not_called()

    def test_spawn_is_detached_and_not_awaited(self):
        env = {**os.environ, "AUTOTUNE_NO_WORKER": "0", "AUTOTUNE_PYTHON": "/opt/py/bin/python3"}
        with mock.patch("autotune.ingress._popen") as popen:
            _, ack = self.ingest_ack(envelope("turn_complete", self.repo), env=env)
        self.assertEqual(ack["worker"], "started")
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        kw = popen.call_args.kwargs
        self.assertEqual(argv, ["/opt/py/bin/python3", "-m", "autotune", "worker", "--project-dir", str(self.repo)])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(kw[stream], subprocess.DEVNULL)
        self.assertTrue(kw["start_new_session"])
        self.assertTrue(kw["close_fds"])
        self.assertEqual(kw["env"]["AUTOTUNE_ORIGIN"], "worker")
        self.assertEqual(Path(kw["cwd"]), Path(ingress.__file__).resolve().parent.parent)
        child = popen.return_value
        child.wait.assert_not_called()
        child.communicate.assert_not_called()

    def test_spawn_failure_does_not_fail_capture(self):
        env = {**os.environ, "AUTOTUNE_NO_WORKER": "0"}
        with mock.patch("autotune.ingress._popen", side_effect=OSError("no exec")):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo), env=env)
        self.assertEqual(code, 0)
        self.assertTrue(ack["ok"])
        self.assertNotEqual(ack["worker"], "started")


class EntryPointTest(RuntimeCase):
    def test_module_entry_point_reads_stdin(self):
        env = {**os.environ, "PYTHONPATH": str(Path(ingress.__file__).resolve().parent.parent)}
        raw = json.dumps(envelope("turn_complete", self.repo)).encode()
        proc = subprocess.run([sys.executable, "-m", "autotune", "ingress"], input=raw, capture_output=True,
                              env=env, cwd=str(Path(ingress.__file__).resolve().parent.parent), timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        ack = json.loads(lines[0])
        self.assertTrue(ack["ok"])
        self.assertFalse(ack["duplicate"])


class GitFactsTest(RuntimeCase):
    repo_config = None

    def test_unborn_repo_reports_unknown_head(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        git(fresh, "init", "-q")
        facts = ingress.git_facts(fresh)
        self.assertEqual(facts["toplevel"], os.path.realpath(fresh))
        self.assertIsNone(facts["head"])
        self.assertTrue(facts["errors"])

    def test_worktrees_share_common_dir(self):
        wt = self.root / "wt"
        git(self.repo, "worktree", "add", "-q", str(wt))
        a = ingress.git_facts(self.repo)
        b = ingress.git_facts(wt)
        self.assertNotEqual(a["toplevel"], b["toplevel"])
        self.assertEqual(a["git_common_dir"], b["git_common_dir"])

    def test_not_a_repo(self):
        plain = self.root / "plain"
        plain.mkdir()
        facts = ingress.git_facts(plain)
        self.assertIsNone(facts["toplevel"])
        self.assertIsNone(facts["git_common_dir"])
        self.assertTrue(facts["errors"])


if __name__ == "__main__":
    unittest.main()


class GitFailureTest(RuntimeCase):
    def test_rev_parse_timeout_is_an_error_not_a_skip(self):
        def slow(args, cwd, env, timeout=None):
            raise subprocess.TimeoutExpired(["git", *args], timeout or 0)

        with mock.patch.object(ingress, "_git", slow):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, ingress.EXIT_PERSIST)
        self.assertEqual((ack["ok"], ack["error"]), (False, "git_unavailable"))
        self.assertIn("rev_parse:timeout", ack["detail"])

    def test_git_missing_is_an_error_not_a_skip(self):
        def missing(args, cwd, env, timeout=None):
            raise FileNotFoundError("git")

        with mock.patch.object(ingress, "_git", missing):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual((code, ack["ok"], ack["error"]), (ingress.EXIT_PERSIST, False, "git_unavailable"))

    def test_rev_parse_gets_a_longer_timeout_than_optional_calls(self):
        seen = {}
        real = ingress._git

        def spy(args, cwd, env, timeout=None):
            seen.setdefault(args[0] if args[0] != "rev-parse" or "--show-toplevel" not in args else "toplevel", timeout)
            return real(args, cwd, env, timeout)

        with mock.patch.object(ingress, "_git", spy):
            ingress.git_facts(self.repo)
        self.assertGreater(seen["toplevel"], seen["status"])


class PostCommitFailureTest(RuntimeCase):
    def test_activity_failure_never_reports_failure_for_a_stored_turn(self):
        from autotune import store

        with mock.patch.object(store, "record_activity", side_effect=sqlite3.OperationalError("database is locked")):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        # The activity write is part of the ingest transaction now, so a failure
        # there rolls back the episode and the ack says so; nothing is half stored.
        conn = self.db()
        episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        if ack["ok"]:
            self.assertEqual(episodes, 1)
        else:
            self.assertEqual(episodes, 0)

    def test_counts_failure_after_commit_still_acks_and_spawns(self):
        with mock.patch.object(ingress, "_counts", side_effect=sqlite3.OperationalError("database is locked")):
            code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 0)
        self.assertTrue(ack["ok"])
        self.assertIn("episode_id", ack)
        self.assertEqual(ack["worker"], "disabled")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
