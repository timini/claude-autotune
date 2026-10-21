"""CLI review, candidate and extended status output."""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from unittest import mock

import autotune
from autotune.util import iso, utc_now

from tests._runtime_support import envelope
from tests.test_runtime_cli import CliCase


@contextmanager
def stub_module(name: str, **attrs):
    """Stand-in for a handler module so these tests do not depend on it."""
    mod = types.ModuleType(f"autotune.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    with mock.patch.dict(sys.modules, {f"autotune.{name}": mod}), \
            mock.patch.object(autotune, name, mod, create=True):
        yield mod


class ReviewCommandTest(CliCase):
    def add_review(self, conn, episode_id, review_id="rv_1", created_at=None, **extra):
        row = {"review_id": review_id, "episode_id": episode_id, "task_stage": "claimed_complete",
               "review_json": "{}", "recommendation": "investigate", "created_at": created_at or iso(utc_now()),
               "status": "valid", "failure_signature": "claims-tests-pass"}
        row.update(extra)
        conn.execute(f"INSERT INTO reviews ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                     list(row.values()))

    def test_review_without_data(self):
        code, out, _ = self.cli("review")
        self.assertEqual(code, 1)
        self.assertIn("no data", out)

    def test_review_passes_the_reference_to_the_formatter(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        seen = []

        def fmt(conn, ref):
            seen.append(ref)
            return None if ref == "missing" else f"REVIEW {ref}"

        with stub_module("review", format_review=fmt):
            self.assertEqual(self.cli("review"), (0, "REVIEW None\n", ""))
            self.assertEqual(self.cli("review", "ep_x")[:2], (0, "REVIEW ep_x\n"))
            code, out, _ = self.cli("review", "missing")
        self.assertEqual(code, 1)
        self.assertIn("No review found for missing", out)
        self.assertEqual(seen, [None, "ep_x", "missing"])

    def test_broken_review_module_only_breaks_the_command(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        saved = autotune.__dict__.pop("review", None)
        try:
            # A None entry in sys.modules makes the import raise ImportError.
            with mock.patch.dict(sys.modules, {"autotune.review": None}):
                code, _, err = self.cli("review")
        finally:
            if saved is not None:
                autotune.review = saved
        self.assertEqual(code, 1)
        self.assertIn("unavailable", err)
        self.assertEqual(self.cli("status")[0], 0)

    def test_status_shows_latest_review_and_model_calls(self):
        _, ack = self.ingest_ack(envelope("turn_complete", self.repo))
        conn = self.db()
        self.add_review(conn, ack["episode_id"], "rv_old", "2026-01-01T00:00:00.000Z")
        self.add_review(conn, ack["episode_id"], "rv_new", "2026-01-02T00:00:00.000Z", job_id=ack["job_id"])
        now = iso(utc_now())
        for i, role in enumerate(("review", "review", "author", "judge")):
            conn.execute("INSERT INTO model_calls (call_id, attempt, role, started_at, outcome) "
                         "VALUES (?, 1, ?, ?, 'ok')", (f"c{i}", role, now))
        conn.execute("INSERT INTO model_calls (call_id, attempt, role, started_at, outcome) "
                     "VALUES ('old', 1, 'review', '2020-01-01T00:00:00.000Z', 'ok')")
        data = self.status_json()
        self.assertEqual(data["latest_review"]["review_id"], "rv_new")
        self.assertEqual(data["model_calls_today"]["review"]["used"], 2)
        self.assertEqual(data["model_calls_today"]["review"]["limit"], 200)
        self.assertEqual(data["model_calls_today"]["experiment"]["used"], 2)
        self.assertEqual(data["model_calls_today"]["experiment"]["roles"]["author"], 1)
        _, out, _ = self.cli("status")
        self.assertIn("Model calls today (UTC): review 2/200", out)
        self.assertIn("experiment 2/50", out)
        self.assertIn("Latest review: rv_new", out)
        self.assertIn("claims-tests-pass", out)


class CandidateCommandTest(CliCase):
    def add_candidate(self, conn, cid, status, created_at, hypothesis="h", reason=None):
        conn.execute(
            "INSERT INTO candidates (candidate_id, hypothesis, effect_metric, allowed_paths_json, change_kind, "
            "base_commit, status, reason, created_at, updated_at) VALUES (?, ?, 'm', '[]', 'instruction', 'abc', "
            "?, ?, ?, ?)",
            (cid, hypothesis, status, reason, created_at, created_at),
        )

    def test_list_without_data_or_candidates(self):
        self.assertEqual(self.cli("candidate")[:2], (0, "No candidates: no data recorded for this project\n"))
        self.ingest_ack(envelope("turn_complete", self.repo))
        self.assertEqual(self.cli("candidate")[:2], (0, "No candidates yet\n"))

    def test_list_is_newest_first_with_short_hypothesis(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        conn = self.db()
        self.add_candidate(conn, "cand_a", "rejected", "2026-01-01T00:00:00.000Z", "old one", "scope_violation")
        self.add_candidate(conn, "cand_b", "validating", "2026-01-02T00:00:00.000Z", "word\n" + "x" * 200)
        code, out, _ = self.cli("candidate")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Recent candidates:")
        self.assertTrue(lines[1].startswith("cand_b  validating  word x"))
        self.assertEqual(len(lines[1].split("  ", 2)[2]), 80)
        self.assertEqual(lines[2], "cand_a  rejected (scope_violation)  old one")
        data = self.status_json()
        self.assertEqual(data["candidates"], {"rejected": 1, "validating": 1})
        self.assertIn("Candidates: 1 rejected, 1 validating", self.cli("status")[1])

    def test_show_one_uses_the_formatter(self):
        self.ingest_ack(envelope("turn_complete", self.repo))
        seen = []

        def fmt(conn, repo_dir, cid):
            seen.append((repo_dir, cid))
            return "CANDIDATE" if cid == "cand_a" else None

        with stub_module("propose", format_candidate=fmt):
            self.assertEqual(self.cli("candidate", "cand_a")[:2], (0, "CANDIDATE\n"))
            code, out, _ = self.cli("candidate", "cand_zz")
        self.assertEqual(code, 1)
        self.assertIn("No candidate cand_zz", out)
        self.assertEqual(seen, [(str(self.repo), "cand_a"), (str(self.repo), "cand_zz")])


class DiskStatusTest(CliCase):
    def test_status_shows_disk_usage_and_fallback_errors(self):
        from autotune import diskcap

        self.ingest_ack(envelope("turn_complete", self.repo))
        diskcap.append_fallback_error(self.data_dir(), "disk_cap", "s", "t", utc_now())
        data = self.status_json()
        self.assertEqual(data["disk"]["cap_bytes"], 1024 * 1024 * 1024)
        self.assertGreater(data["disk"]["db"], 0)
        self.assertFalse(data["disk"]["over_cap"])
        self.assertEqual(data["fallback_capture_errors"], 1)
        _, out, _ = self.cli("status")
        self.assertIn("of 1024.0 MiB cap", out)
        self.assertIn("outside the database (disk cap or disk full): 1", out)
