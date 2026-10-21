"""CLI commands run in-process against a temp repo and data dir."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from autotune import cli, config, queue
from autotune.util import utc_now

from tests._runtime_support import RuntimeCase, envelope, write_config


class CliCase(RuntimeCase):
    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--project-dir", str(self.repo)] if argv and argv[0] != "--version" else list(argv))
        return code, out.getvalue(), err.getvalue()

    def status_json(self) -> dict:
        code, out, _ = self.cli("status", "--json")
        self.assertEqual(code, 0)
        return json.loads(out)


class StatusTest(CliCase):
    def test_status_before_any_capture(self):
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn(str(self.repo), out)
        self.assertIn("Paused: no", out)
        self.assertIn("no data recorded yet", out)
        data = self.status_json()
        self.assertIsNone(data["queue"])
        self.assertEqual(data["enrollment"], "enabled")
        self.assertFalse(self.data_dir().exists(), "status must not create project state")

    def test_status_reports_episodes_queue_and_errors(self):
        _, ack = self.ingest_ack(envelope("turn_complete", self.repo, turn_id="t1"))
        self.ingest_ack(b"{bad")
        big = envelope("turn_complete", self.repo, turn_id="t2")
        big["answer_text"] = "x" * (600 * 1024)
        self.ingest_ack(big)

        data = self.status_json()
        self.assertEqual(data["queue"]["queued"], 1)
        self.assertEqual([e["episode_id"] for e in data["recent_episodes"]], [ack["episode_id"]])
        self.assertEqual(data["recent_episodes"][0]["review_status"], "pending")
        self.assertEqual(data["capture_errors_24h"]["count"], 1)
        self.assertEqual(data["capture_errors_24h"]["recent"][0]["code"], "envelope_too_large")

        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn(ack["episode_id"], out)
        self.assertIn("1 queued", out)
        self.assertIn("envelope_too_large", out)
        self.assertFalse(out.lstrip().startswith("{"), "default status output is text, not JSON")

    def test_status_shows_recent_five_only(self):
        for i in range(7):
            self.ingest_ack(envelope("turn_complete", self.repo, turn_id=f"t{i}"))
        self.assertEqual(len(self.status_json()["recent_episodes"]), 5)

    def test_status_lists_blocked_reasons(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        conn = self.db()
        job = queue.lease(conn, "w", utc_now(), 60)
        queue.block(conn, job["id"], "w", utc_now(), "blocked_auth", "auth_required", "login")
        data = self.status_json()
        self.assertEqual(data["blocked_jobs"][0]["reason"], "auth_required")
        _, out, _ = self.cli("status")
        self.assertIn("auth_required", out)

    def test_status_outside_git_fails(self):
        plain = self.root / "plain"
        plain.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["status", "--project-dir", str(plain)])
        self.assertNotEqual(code, 0)
        self.assertIn("Git", err.getvalue())


class ControlTest(CliCase):
    def test_pause_and_resume_toggle_status(self):
        self.assertEqual(self.cli("pause")[0], 0)
        self.assertTrue(self.status_json()["paused"])
        self.assertIn("Paused: yes", self.cli("status")[1])
        code, out, _ = self.cli("resume")
        self.assertEqual(code, 0)
        self.assertFalse(self.status_json()["paused"])

    def test_pause_does_not_stop_capture(self):
        self.cli("pause")
        code, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(code, 0)
        self.assertFalse(ack["duplicate"])

    def test_pause_requires_enrollment(self):
        (self.repo / ".autotune.json").unlink()
        code, _, err = self.cli("pause")
        self.assertNotEqual(code, 0)
        self.assertIn("enable", err)

    def test_retry_requeues_blocked_jobs(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        conn = self.db()
        job = queue.lease(conn, "w", utc_now(), 60)
        queue.block(conn, job["id"], "w", utc_now(), "blocked_environment", "reviewer_not_configured", None)
        code, out, _ = self.cli("retry")
        self.assertEqual(code, 0)
        self.assertIn("1", out)
        self.assertEqual(conn.execute("SELECT state FROM jobs").fetchone()[0], "queued")

    def test_cancel_queued_and_unknown_jobs(self):
        _, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(self.cli("cancel", str(ack["job_id"]))[0], 0)
        self.assertEqual(self.db().execute("SELECT state FROM jobs").fetchone()[0], "cancelled")
        self.assertNotEqual(self.cli("cancel", str(ack["job_id"]))[0], 0)
        self.assertNotEqual(self.cli("cancel", "9999")[0], 0)

    def test_cancel_running_job_requests_a_stop(self):
        _, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        queue.lease(self.db(), "w", utc_now(), 60)
        code, out, _ = self.cli("cancel", str(ack["job_id"]))
        self.assertEqual(code, 0)
        self.assertIn("Stop requested", out)
        row = self.db().execute("SELECT state, cancel_requested_at FROM jobs").fetchone()
        self.assertEqual(row["state"], "leased")
        self.assertIsNotNone(row["cancel_requested_at"])

    def test_cancel_finished_job_fails(self):
        _, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        conn = self.db()
        job = queue.lease(conn, "w", utc_now(), 60)
        queue.complete(conn, job["id"], "w", utc_now(), None)
        code, out, _ = self.cli("cancel", str(ack["job_id"]))
        self.assertNotEqual(code, 0)
        self.assertIn("succeeded", out)


class EnableTest(CliCase):
    repo_config = None

    def test_enable_writes_valid_default_config(self):
        code, out, _ = self.cli("enable")
        self.assertEqual(code, 0)
        cfg = config.load_config(self.repo)
        self.assertTrue(cfg["enabled"])
        self.assertFalse(cfg["automatic_apply"])
        self.assertEqual(self.status_json()["enrollment"], "enabled")

    def test_enable_never_overwrites(self):
        write_config(self.repo, {"enabled": False, "raw_retention_days": 7})
        before = (self.repo / ".autotune.json").read_bytes()
        code, out, _ = self.cli("enable")
        self.assertEqual(code, 0)
        self.assertEqual((self.repo / ".autotune.json").read_bytes(), before)
        self.assertIn("disabled", out)

    def test_enable_reports_invalid_existing_config(self):
        write_config(self.repo, {"enabled": True, "bogus": 1})
        before = (self.repo / ".autotune.json").read_bytes()
        code, out, _ = self.cli("enable")
        self.assertNotEqual(code, 0)
        self.assertEqual((self.repo / ".autotune.json").read_bytes(), before)


class PurgeTest(CliCase):
    def evidence_with_content(self) -> int:
        return self.db().execute("SELECT COUNT(*) FROM evidence WHERE content IS NOT NULL").fetchone()[0]

    def test_purge_requires_confirmation(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        before = self.evidence_with_content()
        code, out, _ = self.cli("purge", "--scope", "all")
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", out)
        self.assertEqual(self.evidence_with_content(), before)

    def test_purge_evidence_and_all(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertGreater(self.evidence_with_content(), 0)
        self.assertEqual(self.cli("purge", "--scope", "evidence", "--yes")[0], 0)
        self.assertEqual(self.evidence_with_content(), 0)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
        self.assertEqual(self.cli("purge", "--scope", "all", "--yes")[0], 0)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)

    def test_purge_rejects_unknown_scope(self):
        self.assertNotEqual(self.cli("purge", "--scope", "everything", "--yes")[0], 0)


class DoctorTest(CliCase):
    def test_doctor_prints_one_line_per_check(self):
        real_which = shutil.which
        no_claude = lambda name: None if name == "claude" else real_which(name)  # noqa: E731
        with mock.patch("autotune.cli.shutil.which", side_effect=no_claude):
            code, out, _ = self.cli("doctor")
        lines = out.splitlines()
        names = [l.split(":", 1)[0].split(None, 1)[1] for l in lines]
        for expected in ("python", "git", "sqlite", "claude", "data dir", "enrollment"):
            self.assertIn(expected, names)
        for line in lines:
            self.assertRegex(line, r"^(PASS|FAIL)  ")
        self.assertIn("FAIL  claude", out)
        self.assertIn("PASS  enrollment", out)
        self.assertEqual(code, 1)

    def test_doctor_reports_invalid_enrollment(self):
        write_config(self.repo, {"enabled": "yes"})
        code, out, _ = self.cli("doctor")
        self.assertIn("FAIL  enrollment", out)
        self.assertNotEqual(code, 0)


class EntryPointTest(CliCase):
    def test_python_m_autotune_status(self):
        root = Path(cli.__file__).resolve().parent.parent
        env = {**os.environ, "PYTHONPATH": str(root)}
        proc = subprocess.run([sys.executable, "-m", "autotune", "status", "--json", "--project-dir", str(self.repo)],
                              capture_output=True, text=True, env=env, cwd=str(root), timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["project"]["toplevel"], str(self.repo))

    def test_usage_error_is_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(["frobnicate"]), 0)


if __name__ == "__main__":
    unittest.main()
