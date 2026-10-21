import sqlite3
import subprocess
import unittest
from datetime import timedelta

from autotune import queue
from autotune.util import parse_iso

from tests._store_support import T0, TempDBCase

LEASE = 60


class EnqueueTest(TempDBCase):
    def test_dedup_key_returns_existing(self):
        a, created_a = queue.enqueue(self.conn, "propose", "k1", {"x": 1}, T0)
        b, created_b = queue.enqueue(self.conn, "propose", "k1", {"x": 2}, T0)
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_enqueue_joins_caller_transaction(self):
        from autotune import store

        with self.assertRaises(RuntimeError):
            with store.transaction(self.conn):
                queue.enqueue(self.conn, "propose", "k1", {}, T0)
                raise RuntimeError("abort")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            queue.enqueue(self.conn, "bogus", "k", {}, T0)


class LeaseTest(TempDBCase):
    def test_priority_then_fifo(self):
        consolidate, _ = queue.enqueue(self.conn, "consolidate", "c", {}, T0)
        review1, _ = queue.enqueue(self.conn, "review", "r1", {}, T0)
        review2, _ = queue.enqueue(self.conn, "review", "r2", {}, T0)
        apply_, _ = queue.enqueue(self.conn, "apply", "a", {}, T0)
        order = [queue.lease(self.conn, "w", T0, LEASE)["id"] for _ in range(4)]
        self.assertEqual(order, [review1, review2, apply_, consolidate])
        self.assertIsNone(queue.lease(self.conn, "w", T0, LEASE))

    def test_job_type_filter(self):
        queue.enqueue(self.conn, "review", "r", {}, T0)
        p, _ = queue.enqueue(self.conn, "propose", "p", {}, T0)
        self.assertEqual(queue.lease(self.conn, "w", T0, LEASE, job_types=["propose"])["id"], p)
        self.assertIsNone(queue.lease(self.conn, "w", T0, LEASE, job_types=["propose"]))
        self.assertIsNone(queue.lease(self.conn, "w", T0, LEASE, job_types=[]))

    def test_lease_sets_owner_and_attempts(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        row = queue.lease(self.conn, "w1", T0, LEASE)
        self.assertEqual(row["id"], jid)
        self.assertEqual(row["state"], "leased")
        self.assertEqual(row["lease_owner"], "w1")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(parse_iso(row["lease_expires_at"]), T0 + timedelta(seconds=LEASE))

    def test_lease_exclusive_between_connections(self):
        other = self.second_connection()
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        a = queue.lease(self.conn, "w1", T0, LEASE)
        b = queue.lease(other, "w2", T0 + timedelta(seconds=1), LEASE)
        self.assertEqual(a["id"], jid)
        self.assertIsNone(b)
        self.assertFalse(queue.complete(other, jid, "w2", T0, None))
        self.assertTrue(queue.complete(self.conn, jid, "w1", T0, {"ok": True}))

    def test_expired_lease_reclaimed_and_completed_once(self):
        other = self.second_connection()
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        queue.lease(self.conn, "w1", T0, LEASE)
        self.assertIsNone(queue.lease(other, "w2", T0 + timedelta(seconds=LEASE - 1), LEASE))
        later = T0 + timedelta(seconds=LEASE + 1)
        b = queue.lease(other, "w2", later, LEASE)
        self.assertEqual(b["id"], jid)
        self.assertEqual(b["attempts"], 2)
        self.assertEqual(b["last_error"], "lease_expired")
        # The stale owner can no longer finish, heartbeat, retry, block or fail.
        self.assertFalse(queue.complete(self.conn, jid, "w1", later, {"from": "w1"}))
        self.assertFalse(queue.heartbeat(self.conn, jid, "w1", later, LEASE))
        self.assertEqual(queue.retry_later(self.conn, jid, "w1", later, "timeout", None, 5), "lease_lost")
        self.assertFalse(queue.block(self.conn, jid, "w1", later, "blocked_auth", "auth_required", None))
        self.assertFalse(queue.fail(self.conn, jid, "w1", later, "permanent", None))
        self.assertTrue(queue.complete(other, jid, "w2", later, {"from": "w2"}))
        self.assertFalse(queue.complete(other, jid, "w2", later, {"again": True}))
        row = self.job(jid)
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["result_json"], '{"from":"w2"}')
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(queue.lease(self.conn, "w1", later + timedelta(days=1), LEASE))

    def test_heartbeat_extends_lease(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        queue.lease(self.conn, "w1", T0, LEASE)
        t = T0 + timedelta(seconds=LEASE - 5)
        self.assertTrue(queue.heartbeat(self.conn, jid, "w1", t, LEASE))
        self.assertIsNone(queue.lease(self.conn, "w2", T0 + timedelta(seconds=LEASE + 5), LEASE))
        self.assertEqual(self.job(jid)["lease_owner"], "w1")


class RetryTest(TempDBCase):
    def test_backoff_grows_then_fails_at_max_attempts(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        now = T0
        delays = []
        max_attempts = 4
        for attempt in range(1, max_attempts):
            row = queue.lease(self.conn, "w", now, LEASE)
            self.assertEqual(row["id"], jid)
            self.assertEqual(row["attempts"], attempt)
            state = queue.retry_later(
                self.conn, jid, "w", now, "timeout", "slow", max_attempts, base_delay=10, cap=1000, rng=lambda: 1.0
            )
            self.assertEqual(state, "retry_wait")
            job = self.job(jid)
            self.assertEqual(job["last_error"], "timeout")
            eligible = parse_iso(job["next_eligible_at"])
            delays.append((eligible - now).total_seconds())
            # Not eligible before its time.
            self.assertIsNone(queue.lease(self.conn, "w", eligible - timedelta(seconds=1), LEASE))
            now = eligible
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])
        row = queue.lease(self.conn, "w", now, LEASE)
        self.assertEqual(row["attempts"], max_attempts)
        self.assertEqual(queue.retry_later(self.conn, jid, "w", now, "timeout", None, max_attempts), "failed")
        self.assertEqual(self.job(jid)["state"], "failed")
        self.assertIsNone(queue.lease(self.conn, "w", now + timedelta(days=30), LEASE))

    def test_backoff_capped_and_jittered(self):
        self.assertEqual(queue.backoff_delay(50, 30, 3600, rng=lambda: 1.0), 3600)
        low = queue.backoff_delay(3, 30, 3600, rng=lambda: 0.0)
        high = queue.backoff_delay(3, 30, 3600, rng=lambda: 1.0)
        self.assertGreater(low, 0)
        self.assertLess(low, high)


class BlockCancelTest(TempDBCase):
    def test_block_then_unblock(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        queue.lease(self.conn, "w", T0, LEASE)
        self.assertTrue(queue.block(self.conn, jid, "w", T0, "blocked_auth", "auth_required", "login"))
        job = self.job(jid)
        self.assertEqual(job["state"], "blocked_auth")
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(queue.lease(self.conn, "w", T0 + timedelta(days=1), LEASE))
        later = T0 + timedelta(hours=1)
        self.assertEqual(queue.unblock(self.conn, later), 1)
        row = queue.lease(self.conn, "w", later, LEASE)
        self.assertEqual(row["id"], jid)

    def test_unblock_selected_states(self):
        ids = []
        for i, state in enumerate(("blocked_auth", "blocked_budget")):
            jid, _ = queue.enqueue(self.conn, "review", f"r{i}", {}, T0)
            queue.lease(self.conn, "w", T0, LEASE)
            queue.block(self.conn, jid, "w", T0, state, "x", None)
            ids.append(jid)
        self.assertEqual(queue.unblock(self.conn, T0, states=("blocked_budget",)), 1)
        self.assertEqual(self.job(ids[0])["state"], "blocked_auth")
        self.assertEqual(self.job(ids[1])["state"], "queued")

    def test_block_rejects_non_blocked_state(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        queue.lease(self.conn, "w", T0, LEASE)
        with self.assertRaises(ValueError):
            queue.block(self.conn, jid, "w", T0, "failed", "x", None)

    def test_cancel(self):
        queued, _ = queue.enqueue(self.conn, "review", "q", {}, T0)
        leased, _ = queue.enqueue(self.conn, "propose", "l", {}, T0)
        self.assertTrue(queue.cancel(self.conn, queued, T0))
        self.assertEqual(self.job(queued)["state"], "cancelled")
        self.assertFalse(queue.cancel(self.conn, queued, T0))
        row = queue.lease(self.conn, "w", T0, LEASE)
        self.assertEqual(row["id"], leased)
        self.assertFalse(queue.cancel_requested(self.conn, leased))
        self.assertTrue(queue.cancel(self.conn, leased, T0))
        self.assertEqual(self.job(leased)["state"], "leased")
        self.assertTrue(queue.cancel_requested(self.conn, leased))
        # The stop request wins over the block the worker records afterwards.
        self.assertTrue(queue.block(self.conn, leased, "w", T0, "blocked_environment", "cli_missing", None))
        self.assertEqual(self.job(leased)["state"], "cancelled")
        self.assertFalse(queue.cancel(self.conn, leased, T0))
        self.assertIsNone(queue.lease(self.conn, "w", T0 + timedelta(days=1), LEASE))
        self.assertFalse(queue.cancel(self.conn, 99999, T0))


class StopRequestRaceTest(TempDBCase):
    """A stop requested after the worker checked the flag still wins."""

    def leased_with_stop(self, key="r"):
        jid, _ = queue.enqueue(self.conn, "review", key, {}, T0)
        queue.lease(self.conn, "w", T0, LEASE)
        self.assertTrue(queue.cancel(self.conn, jid, T0))
        return jid

    def test_retry_becomes_cancelled(self):
        jid = self.leased_with_stop()
        self.assertEqual(queue.retry_later(self.conn, jid, "w", T0, "timeout", None, 5), "cancelled")
        self.assertEqual(self.job(jid)["state"], "cancelled")
        jid = self.leased_with_stop("d")
        self.assertEqual(queue.retry_later(self.conn, jid, "w", T0, "candidate_slot_busy", None, 5, delay=300),
                         "cancelled")
        self.assertIsNone(queue.lease(self.conn, "w", T0 + timedelta(days=1), LEASE))

    def test_fail_becomes_cancelled(self):
        jid = self.leased_with_stop()
        self.assertTrue(queue.fail(self.conn, jid, "w", T0, "permanent", None))
        self.assertEqual(self.job(jid)["state"], "cancelled")

    def test_queued_job_with_a_stop_request_is_never_leased(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        self.conn.execute("UPDATE jobs SET cancel_requested_at = ? WHERE id = ?", ("2026-01-01T00:00:00.000Z", jid))
        self.assertIsNone(queue.lease(self.conn, "w", T0, LEASE))
        self.assertEqual(self.job(jid)["state"], "cancelled")

    def test_expired_stopped_lease_at_max_attempts_is_cancelled(self):
        jid, _ = queue.enqueue(self.conn, "review", "r", {}, T0)
        queue.lease(self.conn, "w", T0, LEASE, max_attempts=1)
        queue.cancel(self.conn, jid, T0)
        queue.lease(self.conn, "w", T0 + timedelta(seconds=LEASE + 1), LEASE, max_attempts=1)
        self.assertEqual(self.job(jid)["state"], "cancelled")


class ClassifyTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(queue.classify_error("timeout"), "transient")
        self.assertEqual(queue.classify_error("auth_required"), "auth_required")
        self.assertEqual(queue.classify_error("usage_limit"), "budget_exhausted")
        self.assertEqual(queue.classify_error("reviewer_not_configured"), "environment")
        self.assertEqual(queue.classify_error("something_new"), "permanent")

    def test_exceptions(self):
        self.assertEqual(queue.classify_error(sqlite3.OperationalError("database is locked")), "transient")
        self.assertEqual(queue.classify_error(subprocess.TimeoutExpired(["x"], 1)), "transient")
        self.assertEqual(queue.classify_error(FileNotFoundError("claude")), "environment")
        self.assertEqual(queue.classify_error(ValueError("bad")), "permanent")

        class CodedError(Exception):
            code = "auth_required"

        self.assertEqual(queue.classify_error(CodedError()), "auth_required")


if __name__ == "__main__":
    unittest.main()


class LeaseExpiryAttemptsTest(TempDBCase):
    def test_repeated_lease_expiry_fails_after_max_attempts(self):
        jid, _ = queue.enqueue(self.conn, "review", "crashy", {}, T0)
        now = T0
        for _ in range(12):
            queue.lease(self.conn, "w", now, LEASE, max_attempts=5)
            now += timedelta(seconds=LEASE + 1)
        queue.lease(self.conn, "w", now, LEASE, max_attempts=5)
        row = self.conn.execute("SELECT state, attempts, last_error FROM jobs WHERE id = ?", (jid,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 5)
        self.assertEqual(row["last_error"], "lease_expired_max_attempts")

    def test_expiry_below_max_attempts_requeues(self):
        jid, _ = queue.enqueue(self.conn, "review", "once", {}, T0)
        queue.lease(self.conn, "w", T0, LEASE, max_attempts=5)
        row = queue.lease(self.conn, "w", T0 + timedelta(seconds=LEASE + 1), LEASE, max_attempts=5)
        self.assertEqual((row["id"], row["attempts"]), (jid, 2))


class RateLimitClassTest(unittest.TestCase):
    def test_rate_limited_is_transient(self):
        self.assertEqual(queue.classify_error("rate_limited"), "transient")
        self.assertEqual(queue.classify_error("usage_limit"), "budget_exhausted")
        self.assertEqual(queue.classify_error("quota_exceeded"), "budget_exhausted")


class CommitTest(TempDBCase):
    def leased(self, key="c"):
        jid, _ = queue.enqueue(self.conn, "review", key, {}, T0)
        self.assertEqual(queue.lease(self.conn, "w", T0, LEASE)["id"], jid)
        return jid

    def meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def test_on_commit_runs_with_the_completion(self):
        jid = self.leased()

        def write(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('handler', 'wrote')")

        self.assertTrue(queue.complete(self.conn, jid, "w", T0, {"ok": 1}, on_commit=write))
        self.assertEqual(self.job(jid)["state"], "succeeded")
        self.assertEqual(self.meta("handler"), "wrote")

    def test_on_commit_failure_rolls_back_both(self):
        jid = self.leased()

        def write_then_fail(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('handler', 'wrote')")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            queue.complete(self.conn, jid, "w", T0, None, on_commit=write_then_fail)
        self.assertIsNone(self.meta("handler"))
        row = self.job(jid)
        self.assertEqual(row["state"], "leased")
        self.assertEqual(row["lease_owner"], "w")

    def test_on_commit_skipped_without_the_lease(self):
        jid = self.leased()
        called = []
        self.assertFalse(queue.complete(self.conn, jid, "other", T0, None, on_commit=called.append))
        self.assertEqual(called, [])
        self.assertEqual(self.job(jid)["state"], "leased")

    def test_finish_cancelled_needs_the_lease(self):
        jid = self.leased()
        self.assertFalse(queue.finish_cancelled(self.conn, jid, "other", T0, "x"))
        self.assertTrue(queue.finish_cancelled(self.conn, jid, "w", T0, "stopped"))
        row = self.job(jid)
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["last_error"], "cancelled")
        self.assertIsNone(row["lease_owner"])

    def test_expired_lease_with_stop_request_is_cancelled_not_requeued(self):
        jid = self.leased()
        self.assertTrue(queue.cancel(self.conn, jid, T0))
        self.assertIsNone(queue.lease(self.conn, "w2", T0 + timedelta(seconds=LEASE + 1), LEASE))
        self.assertEqual(self.job(jid)["state"], "cancelled")


class DeferTest(TempDBCase):
    def test_explicit_delay_defers_without_using_an_attempt(self):
        jid, _ = queue.enqueue(self.conn, "propose", "p", {}, T0)
        for i in range(4):
            now = T0 + timedelta(hours=i)
            self.assertEqual(queue.lease(self.conn, "w", now, LEASE)["id"], jid)
            state = queue.retry_later(self.conn, jid, "w", now, "candidate_slot_busy", None, 2, delay=300)
            self.assertEqual(state, "retry_wait")
            row = self.job(jid)
            self.assertEqual(row["attempts"], 0)
            self.assertEqual(parse_iso(row["next_eligible_at"]), now + timedelta(seconds=300))
            self.assertEqual(row["last_error"], "candidate_slot_busy")

    def test_unblock_updated_before_only_touches_older_jobs(self):
        old, _ = queue.enqueue(self.conn, "review", "old", {}, T0)
        new, _ = queue.enqueue(self.conn, "review", "new", {}, T0)
        for jid, ts in ((old, T0), (new, T0 + timedelta(days=1))):
            queue.lease(self.conn, "w", ts, LEASE)
            queue.block(self.conn, jid, "w", ts, "blocked_budget", "budget_exhausted", None)
        cutoff = T0 + timedelta(hours=12)
        self.assertEqual(queue.unblock(self.conn, cutoff + timedelta(days=1), states=("blocked_budget",),
                                       updated_before=cutoff), 1)
        self.assertEqual(self.job(old)["state"], "queued")
        self.assertEqual(self.job(new)["state"], "blocked_budget")


class ContractCodesTest(unittest.TestCase):
    def test_cycle1_codes_are_classified(self):
        expected = {
            "cancelled": "permanent",
            "commit_failed": "transient",
            "invalid_evidence_reference": "permanent",
            "candidate_slot_busy": "transient",
            "checkout_dirty": "transient",
            "scope_violation": "permanent",
            "needs_scope_change": "permanent",
            "base_changed": "permanent",
            "no_change": "permanent",
            "foreground_active": "transient",
            "disk_cap": "environment",
            "handler_unavailable": "environment",
            "git_timeout": "transient",
            "rate_limited": "transient",
            "overloaded": "transient",
            "server_error": "transient",
            "usage_limit": "budget_exhausted",
            "budget_exhausted": "budget_exhausted",
            "cli_missing": "environment",
            "cli_incompatible": "environment",
            "invalid_output": "permanent",
        }
        for code, klass in expected.items():
            with self.subTest(code=code):
                self.assertIn(code, queue._CODE_CLASSES)
                self.assertEqual(queue.classify_error(code), klass)
