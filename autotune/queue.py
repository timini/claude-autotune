"""Durable job queue with leases, retries, and blocked states.

All time comparisons use ISO strings written by util.iso, which sort lexically.
"""

from __future__ import annotations

import random
import sqlite3
import subprocess
from datetime import datetime, timedelta
from typing import Callable

from .store import BLOCKED_STATES, transaction
from .util import canonical_json, iso

PRIORITIES = {
    "review": 10,
    "apply": 20,
    "verify_active": 25,
    "reproduce_regression": 30,
    "propose": 40,
    "evaluate": 50,
    "consolidate": 60,
}

STATES = (
    "queued",
    "leased",
    "succeeded",
    "retry_wait",
    "blocked_budget",
    "blocked_auth",
    "blocked_environment",
    "failed",
    "cancelled",
)

CANCELLABLE_STATES = ("queued", "retry_wait") + BLOCKED_STATES


def enqueue(
    conn: sqlite3.Connection,
    job_type: str,
    dedup_key: str,
    payload: dict,
    now: datetime,
    priority: int | None = None,
    *,
    episode_id: str | None = None,
    candidate_id: str | None = None,
) -> tuple[int, bool]:
    if job_type not in PRIORITIES:
        raise ValueError(f"unknown job type {job_type!r}")
    if priority is None:
        priority = PRIORITIES[job_type]
    ts = iso(now)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO jobs (job_type, dedup_key, episode_id, candidate_id, payload_json, "
            "priority, state, next_eligible_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?) ON CONFLICT (dedup_key) DO NOTHING",
            (
                job_type,
                dedup_key,
                episode_id,
                candidate_id,
                canonical_json(payload or {}),
                priority,
                ts,
                ts,
                ts,
            ),
        )
        if cur.rowcount == 1:
            return cur.lastrowid, True
        row = conn.execute("SELECT id FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
        return row["id"], False


def _reclaim(conn, ts: str, max_attempts: int | None = None) -> None:
    # A stop was requested and the worker died before honouring it. This runs
    # before the attempt limit so a stopped job ends cancelled, not failed.
    conn.execute(
        "UPDATE jobs SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL, "
        "last_error = 'cancelled', last_error_detail = 'lease expired after a stop request', updated_at = ? "
        "WHERE state = 'leased' AND lease_expires_at <= ? AND cancel_requested_at IS NOT NULL",
        (ts, ts),
    )
    # A stop that arrived while the worker was recording a retry or a block.
    conn.execute(
        f"UPDATE jobs SET state = 'cancelled', last_error = 'cancelled', updated_at = ? "
        f"WHERE cancel_requested_at IS NOT NULL AND state IN ({', '.join('?' for _ in CANCELLABLE_STATES)})",
        (ts, *CANCELLABLE_STATES),
    )
    # A worker that dies inside a handler never calls retry_later, so the
    # attempt limit is enforced here too or a crashing job loops forever.
    if max_attempts is not None:
        conn.execute(
            "UPDATE jobs SET state = 'failed', lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = 'lease_expired_max_attempts', last_error_detail = NULL, updated_at = ? "
            "WHERE state = 'leased' AND lease_expires_at <= ? AND attempts >= ?",
            (ts, ts, max_attempts),
        )
    conn.execute(
        "UPDATE jobs SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
        "last_error = 'lease_expired', last_error_detail = NULL, next_eligible_at = ?, updated_at = ? "
        "WHERE state = 'leased' AND lease_expires_at <= ?",
        (ts, ts, ts),
    )
    conn.execute(
        "UPDATE jobs SET state = 'queued', updated_at = ? "
        "WHERE state = 'retry_wait' AND next_eligible_at <= ?",
        (ts, ts),
    )


def lease(
    conn: sqlite3.Connection,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
    job_types=None,
    *,
    max_attempts: int | None = None,
) -> sqlite3.Row | None:
    ts = iso(now)
    expires = iso(now + timedelta(seconds=lease_seconds))
    with transaction(conn):
        _reclaim(conn, ts, max_attempts)
        sql = "SELECT id FROM jobs WHERE state = 'queued' AND next_eligible_at <= ?"
        args: list = [ts]
        if job_types is not None:
            types = list(job_types)
            if not types:
                return None
            sql += f" AND job_type IN ({', '.join('?' for _ in types)})"
            args.extend(types)
        sql += " ORDER BY priority, id LIMIT 1"
        row = conn.execute(sql, args).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET state = 'leased', lease_owner = ?, lease_expires_at = ?, "
            "heartbeat_at = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (worker_id, expires, ts, ts, row["id"]),
        )
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()


def _owned_update(conn, job_id, worker_id, set_sql: str, args) -> bool:
    with transaction(conn):
        cur = conn.execute(
            f"UPDATE jobs SET {set_sql} WHERE id = ? AND state = 'leased' AND lease_owner = ?",
            (*args, job_id, worker_id),
        )
        return cur.rowcount == 1


def heartbeat(conn, job_id: int, worker_id: str, now: datetime, lease_seconds: int) -> bool:
    ts = iso(now)
    return _owned_update(
        conn,
        job_id,
        worker_id,
        "heartbeat_at = ?, lease_expires_at = ?, updated_at = ?",
        (ts, iso(now + timedelta(seconds=lease_seconds)), ts),
    )


def complete(
    conn,
    job_id: int,
    worker_id: str,
    now: datetime,
    result: dict | None,
    *,
    on_commit: Callable[[sqlite3.Connection], None] | None = None,
) -> bool:
    """Mark the job succeeded if this worker still owns it.

    `on_commit(conn)` runs in the same transaction, only after the ownership
    update matched. If it raises, everything rolls back and the exception
    propagates; the job stays leased by this worker.
    """
    ts = iso(now)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET state = 'succeeded', lease_owner = NULL, lease_expires_at = NULL, "
            "result_json = ?, updated_at = ? WHERE id = ? AND state = 'leased' AND lease_owner = ?",
            (canonical_json(result) if result is not None else None, ts, job_id, worker_id),
        )
        if cur.rowcount != 1:
            return False
        if on_commit is not None:
            on_commit(conn)
        return True


def finish_cancelled(conn, job_id: int, worker_id: str, now: datetime, detail=None) -> bool:
    ts = iso(now)
    return _owned_update(
        conn,
        job_id,
        worker_id,
        "state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL, last_error = 'cancelled', "
        "last_error_detail = ?, updated_at = ?",
        (_detail(detail), ts),
    )


def cancel_requested(conn, job_id: int) -> bool:
    row = conn.execute("SELECT cancel_requested_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None and row["cancel_requested_at"] is not None


def backoff_delay(attempts: int, base_delay: float, cap: float, rng=random.random) -> float:
    """Exponential delay for the given attempt count with jitter in [50%, 100%]."""
    exp = min(cap, base_delay * (2 ** max(0, attempts - 1)))
    return exp * (0.5 + 0.5 * rng())


def retry_later(
    conn,
    job_id: int,
    worker_id: str,
    now: datetime,
    error_code: str,
    detail,
    max_attempts: int,
    base_delay: float = 30,
    cap: float = 3600,
    rng=random.random,
    *,
    delay: float | None = None,
) -> str:
    """Schedule a retry, or fail when attempts are used up.

    With an explicit `delay` the job is deferred rather than retried: it waits
    that long, and the attempt it just used is given back, so a deferral never
    exhausts max_attempts.

    Returns "retry_wait", "failed", "cancelled" when a stop was requested
    meanwhile, or "lease_lost" when the caller no longer holds the lease
    (nothing is changed in that case).
    """
    ts = iso(now)
    with transaction(conn):
        row = conn.execute(
            "SELECT attempts, cancel_requested_at FROM jobs WHERE id = ? AND state = 'leased' AND lease_owner = ?",
            (job_id, worker_id),
        ).fetchone()
        if row is None:
            return "lease_lost"
        if row["cancel_requested_at"] is not None:
            _set_cancelled(conn, job_id, ts, error_code)
            return "cancelled"
        attempts = row["attempts"]
        give_back = 0
        if delay is not None:
            state, eligible, give_back = "retry_wait", iso(now + timedelta(seconds=max(0.0, delay))), 1
        elif attempts >= max_attempts:
            state, eligible = "failed", ts
        else:
            wait = backoff_delay(attempts, base_delay, cap, rng)
            state, eligible = "retry_wait", iso(now + timedelta(seconds=wait))
        conn.execute(
            "UPDATE jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL, "
            "next_eligible_at = ?, last_error = ?, last_error_detail = ?, updated_at = ?, "
            "attempts = MAX(0, attempts - ?) WHERE id = ?",
            (state, eligible, error_code, _detail(detail), ts, give_back, job_id),
        )
        return state


def _set_cancelled(conn, job_id: int, ts: str, error_code) -> None:
    conn.execute(
        "UPDATE jobs SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL, "
        "last_error = 'cancelled', last_error_detail = ?, updated_at = ? WHERE id = ?",
        (_detail(f"stop requested; handler ended with {error_code}"), ts, job_id),
    )


def _end(conn, job_id: int, worker_id: str, ts: str, state: str, error_code: str, detail) -> bool:
    """Move an owned lease to `state`, or to cancelled when a stop was
    requested; the check and the write share one transaction."""
    with transaction(conn):
        row = conn.execute(
            "SELECT cancel_requested_at FROM jobs WHERE id = ? AND state = 'leased' AND lease_owner = ?",
            (job_id, worker_id),
        ).fetchone()
        if row is None:
            return False
        if row["cancel_requested_at"] is not None:
            _set_cancelled(conn, job_id, ts, error_code)
            return True
        conn.execute(
            "UPDATE jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL, last_error = ?, "
            "last_error_detail = ?, updated_at = ? WHERE id = ?",
            (state, error_code, _detail(detail), ts, job_id),
        )
        return True


def block(conn, job_id: int, worker_id: str, now: datetime, state: str, error_code: str, detail) -> bool:
    """Block an owned job (cancel it instead when a stop was requested)."""
    if state not in BLOCKED_STATES:
        raise ValueError(f"not a blocked state: {state!r}")
    return _end(conn, job_id, worker_id, iso(now), state, error_code, detail)


def fail(conn, job_id: int, worker_id: str, now: datetime, error_code: str, detail) -> bool:
    """Fail an owned job (cancel it instead when a stop was requested)."""
    return _end(conn, job_id, worker_id, iso(now), "failed", error_code, detail)


def cancel(conn, job_id: int, now: datetime) -> bool:
    """Cancel a job that is not running, or request a stop for a leased one.

    Returns True when the job was cancelled or a stop was requested. A leased
    job keeps its state until its worker notices the request.
    """
    ts = iso(now)
    with transaction(conn):
        cur = conn.execute(
            f"UPDATE jobs SET state = 'cancelled', updated_at = ? WHERE id = ? "
            f"AND state IN ({', '.join('?' for _ in CANCELLABLE_STATES)})",
            (ts, job_id, *CANCELLABLE_STATES),
        )
        if cur.rowcount == 1:
            return True
        cur = conn.execute(
            "UPDATE jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) "
            "WHERE id = ? AND state = 'leased'",
            (ts, job_id),
        )
        return cur.rowcount == 1


def unblock(conn, now: datetime, states=BLOCKED_STATES, *, updated_before: datetime | None = None) -> int:
    """Requeue blocked jobs. With `updated_before`, only jobs last updated earlier."""
    states = tuple(states)
    for s in states:
        if s not in BLOCKED_STATES:
            raise ValueError(f"not a blocked state: {s!r}")
    if not states:
        return 0
    ts = iso(now)
    sql = (
        f"UPDATE jobs SET state = 'queued', next_eligible_at = ?, updated_at = ? "
        f"WHERE state IN ({', '.join('?' for _ in states)})"
    )
    args: list = [ts, ts, *states]
    if updated_before is not None:
        sql += " AND updated_at < ?"
        args.append(iso(updated_before))
    with transaction(conn):
        return conn.execute(sql, args).rowcount


def _detail(detail) -> str | None:
    if detail is None:
        return None
    return str(detail)[:2000]


_CODE_CLASSES = {
    "transient": "transient",
    "timeout": "transient",
    "lease_expired": "transient",
    "database_locked": "transient",
    "network_error": "transient",
    "overloaded": "transient",
    "server_error": "transient",
    "auth_required": "auth_required",
    "not_logged_in": "auth_required",
    "unauthorized": "auth_required",
    "auth_expired": "auth_required",
    "budget_exhausted": "budget_exhausted",
    "usage_limit": "budget_exhausted",
    "rate_limited": "transient",
    "quota_exceeded": "budget_exhausted",
    "environment": "environment",
    "reviewer_not_configured": "environment",
    "cli_missing": "environment",
    "cli_incompatible": "environment",
    "git_unavailable": "environment",
    "disk_full": "environment",
    "handler_unavailable": "environment",
    "disk_cap": "environment",
    "git_timeout": "transient",
    "commit_failed": "transient",
    "candidate_slot_busy": "transient",
    "checkout_dirty": "transient",
    "foreground_active": "transient",
    "permanent": "permanent",
    "invalid_output": "permanent",
    "evidence_expired": "permanent",
    "cancelled": "permanent",
    "invalid_evidence_reference": "permanent",
    "episode_mismatch": "permanent",
    "invalid_handler_result": "permanent",
    "scope_violation": "permanent",
    "needs_scope_change": "permanent",
    "base_changed": "permanent",
    "no_change": "permanent",
    "git_failed": "permanent",
    "worktree_git_tampered": "permanent",
    "checkout_modified": "permanent",
    # Cycle 2: evaluate, apply, revert and replay.
    "apply_lock_busy": "transient",
    "apply_paused": "transient",
    "branch_changed": "transient",
    "experiment_lock_busy": "transient",
    "exposure_unknown": "transient",
    "revert_head_moved": "transient",
    "candidate_commit_missing": "permanent",
    "git_reconciliation_required": "permanent",
    "revert_conflict": "permanent",
    "revert_scope_violation": "permanent",
    "invalid_case": "permanent",
    "workspace_unsafe": "environment",
}


def classify_error(exc_or_code) -> str:
    """Map an exception or error code to transient, auth_required,
    budget_exhausted, environment, or permanent. Unknown codes are permanent."""
    if isinstance(exc_or_code, str):
        return _CODE_CLASSES.get(exc_or_code, "permanent")
    code = getattr(exc_or_code, "code", None)
    if isinstance(code, str) and code in _CODE_CLASSES:
        return _CODE_CLASSES[code]
    exc = exc_or_code
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return "transient"
        if "disk" in msg or "full" in msg:
            return "environment"
        return "permanent"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired, ConnectionError, InterruptedError)):
        return "transient"
    if isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError)):
        return "environment"
    if isinstance(exc, OSError):
        return "environment"
    return "permanent"
