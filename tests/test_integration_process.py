"""Ingress and worker as real processes: detach, races, ack loss, parent
exit, recursion guards and the status command."""

from __future__ import annotations

import json
import subprocess
import textwrap

from autotune import locks, paths
from tests._integration_handlers import count_lines
from tests._integration_support import PLUGIN_ROOT, PYTHON, IntegrationCase, pid_alive, wait_for
from tests._runtime_support import make_repo


class DetachTest(IntegrationCase):
    repo_config = {"enabled": True, "worker_idle_seconds": 3}

    def test_ingress_returns_while_worker_stays_alive(self):
        code, ack, out, elapsed = self.run_ingress(self.envelope(), env=self.sub_env(worker=True))
        self.assertEqual(code, 0, out)
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["worker"], "started")
        self.assertLess(elapsed, 1.5, "ingress waited on the worker or its pipes")

        lock_path = paths.worker_lock_path(self.data_dir())
        self.assertTrue(wait_for(lambda: locks.is_locked(lock_path), 3.0), "worker never took its lock")
        pid = locks.holder_pid(lock_path)
        self.assertIsNotNone(pid)
        self.track_pid(pid)
        self.assertTrue(pid_alive(pid))

        # The default review handler finds no CLI and blocks the job, then the worker idles out.
        self.assertTrue(wait_for(lambda: not pid_alive(pid), 10.0), "worker did not exit by itself")
        self.assertFalse(locks.is_locked(lock_path))
        state = self.scalar("SELECT state FROM jobs WHERE id = ?", (ack["job_id"],))
        self.assertEqual(state, "blocked_environment")


class RaceTest(IntegrationCase):
    N = 8

    def test_concurrent_identical_envelopes_make_one_episode(self):
        raw = json.dumps(self.envelope(session_id="race-s", turn_id="race-t")).encode()
        procs = [
            subprocess.Popen([PYTHON, "-m", "autotune", "ingress"], cwd=PLUGIN_ROOT, env=self.sub_env(),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(self.N)
        ]
        # Feed every stdin before collecting any output, so the runs overlap.
        for p in procs:
            p.stdin.write(raw)
            p.stdin.close()
            # communicate() would flush the closed pipe (ValueError on 3.11).
            p.stdin = None
        acks = []
        for p in procs:
            out, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, err.decode())
            acks.append(json.loads(out))
        self.assertTrue(all(a["ok"] for a in acks), acks)
        self.assertEqual(len({a["episode_id"] for a in acks}), 1, acks)
        self.assertEqual(len({a["job_id"] for a in acks}), 1, acks)
        self.assertEqual(sum(1 for a in acks if not a["duplicate"]), 1, acks)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM episodes"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM jobs"), 1)


class AckLossTest(IntegrationCase):
    def test_resend_returns_duplicate_with_same_ids(self):
        env = self.envelope(session_id="ack-s", turn_id="ack-t")
        code1, first, out1, _ = self.run_ingress(env)
        code2, second, out2, _ = self.run_ingress(env)
        self.assertEqual((code1, code2), (0, 0), out1 + out2)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["episode_id"], second["episode_id"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM episodes"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM jobs"), 1)


_PARENT = textwrap.dedent(
    """
    import os, subprocess, sys
    p = subprocess.Popen([sys.executable, "-m", "autotune", "ingress"], cwd=sys.argv[1],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.stdin.write(sys.stdin.buffer.read())
    p.stdin.close()
    print(p.pid, flush=True)
    os._exit(0)
    """
)


class ParentExitTest(IntegrationCase):
    repo_config = {"enabled": True, "worker_idle_seconds": 1}

    def test_queued_work_survives_parent_exit(self):
        env = self.sub_env(worker=True, AUTOTUNE_TEST_HANDLERS="tests._integration_handlers")
        raw = json.dumps(self.envelope(session_id="pe-s", turn_id="pe-t")).encode()
        parent = subprocess.run([PYTHON, "-c", _PARENT, str(PLUGIN_ROOT)], input=raw, cwd=PLUGIN_ROOT,
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(parent.returncode, 0, parent.stderr.decode())
        self.track_pid(int(parent.stdout.strip()))

        self.assertTrue(wait_for(lambda: count_lines(self.test_dir, "processed"), 10.0),
                        "worker never processed the job after the parent exited")
        job_ids = count_lines(self.test_dir, "processed")
        self.assertEqual(len(job_ids), 1)
        lock_path = paths.worker_lock_path(self.data_dir())
        self.assertTrue(wait_for(lambda: not locks.is_locked(lock_path), 10.0))
        rows = self.query("SELECT id, state, attempts FROM jobs")
        self.assertEqual([(r["id"], r["state"], r["attempts"]) for r in rows], [(int(job_ids[0]), "succeeded", 1)])

    def test_test_handlers_ignored_outside_tests_package(self):
        from autotune import cli

        self.assertIsNone(cli._test_handlers({"AUTOTUNE_TEST_HANDLERS": "os", "AUTOTUNE_HOME": "/x"}))
        self.assertIsNone(cli._test_handlers({"AUTOTUNE_TEST_HANDLERS": "tests._integration_handlers"}))
        self.assertIsNotNone(cli._test_handlers({"AUTOTUNE_TEST_HANDLERS": "tests._integration_handlers",
                                                 "AUTOTUNE_HOME": "/x"}))


class RecursionTest(IntegrationCase):
    def test_worker_origin_envelope_is_skipped(self):
        code, ack, out, _ = self.run_ingress(self.envelope(origin="worker"))
        self.assertEqual(code, 0, out)
        self.assertEqual(ack, {"ok": True, "skipped": "non_foreground_origin"})
        self.assertFalse(self.home.exists())

    def test_worker_process_env_is_skipped(self):
        code, ack, out, _ = self.run_ingress(self.envelope(), env=self.sub_env(AUTOTUNE_ORIGIN="worker"))
        self.assertEqual(code, 0, out)
        self.assertEqual(ack, {"ok": True, "skipped": "non_foreground_origin"})
        self.assertFalse(self.home.exists())

    def test_unenrolled_repo_is_skipped_without_data_dir(self):
        other_root = self.root / "other"
        other_root.mkdir()
        other = make_repo(other_root, None)
        env = self.envelope()
        env["cwd"] = str(other)
        code, ack, out, _ = self.run_ingress(env)
        self.assertEqual(code, 0, out)
        self.assertEqual(ack, {"ok": True, "skipped": "not_enrolled"})
        self.assertFalse(self.home.exists())


class StatusTest(IntegrationCase):
    def test_status_shows_queue_and_recent_episodes(self):
        ids = []
        for i in range(3):
            code, ack, out, _ = self.run_ingress(self.envelope(session_id="st-s", turn_id=f"st-t{i}"))
            self.assertEqual(code, 0, out)
            ids.append(ack["episode_id"])
        proc = self.run_cli("status", "--project-dir", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Queue: 3 queued", proc.stdout)
        for ep in ids:
            self.assertIn(ep, proc.stdout)

        proc = self.run_cli("status", "--json", "--project-dir", str(self.repo))
        data = json.loads(proc.stdout)
        self.assertEqual(data["queue"]["queued"], 3)
        self.assertEqual({e["episode_id"] for e in data["recent_episodes"]}, set(ids))
