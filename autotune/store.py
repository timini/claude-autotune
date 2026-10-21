"""SQLite connection, transactions, and episode ingest."""

from __future__ import annotations

import os
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .util import canonical_json, iso

SCHEMA_VERSION = 3
DEFAULT_RAW_RETENTION_DAYS = 30
REVIEW_PRIORITY = 10

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_BEGIN_ATTEMPTS = 20
_BEGIN_BACKOFF_BUDGET = 1.5  # seconds of our own sleeping, on top of busy_timeout

_EPISODE_COLUMNS = (
    "episode_id",
    "session_id",
    "turn_id",
    "lifecycle_id",
    "lifecycle_source",
    "origin",
    "task_id",
    "started_at",
    "completed_at",
    "captured_at",
    "end_reason",
    "duration_ms",
    "model",
    "transcript_path",
    "repository_json",
    "loaded_harness_json",
    "coverage_json",
    "usage_json",
)

_EVIDENCE_COLUMNS = (
    "evidence_id",
    "position",
    "kind",
    "role",
    "content",
    "content_sha256",
    "provenance_json",
    "truncated",
)

BLOCKED_STATES = ("blocked_budget", "blocked_auth", "blocked_environment")

# Columns added in schema version 2, as (table, column, definition).
_V2_COLUMNS = (
    ("jobs", "cancel_requested_at", "TEXT"),
    ("reviews", "status", "TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'rejected'))"),
    ("reviews", "adjustments_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("reviews", "input_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("reviews", "omitted_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("reviews", "failure_signature", "TEXT"),
    ("reviews", "session_id", "TEXT"),
    ("candidates", "worktree_path", "TEXT"),
    ("candidates", "author_call_id", "TEXT"),
    ("candidates", "author_summary", "TEXT"),
    ("candidates", "validation_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("candidates", "source_sessions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("model_calls", "cost_usd", "REAL"),
    ("model_calls", "duration_ms", "INTEGER"),
    ("model_calls", "cli_session_id", "TEXT"),
)

# Columns added in schema version 3 (evaluation, application, recovery).
_V3_COLUMNS = (
    ("candidates", "gate_json", "TEXT"),
    ("candidates", "target_branch", "TEXT"),
    ("candidates", "task_family", "TEXT"),
    ("candidates", "applied_at", "TEXT"),
    ("candidates", "reverted_at", "TEXT"),
    ("candidates", "branch_deleted_at", "TEXT"),
    ("candidates", "exposure", "TEXT"),
    ("evaluation_runs", "error_code", "TEXT"),
    ("evaluation_runs", "display_order", "INTEGER"),
    ("evaluation_runs", "duration_ms", "INTEGER"),
)

# Columns each version adds over the one before it.
_MIGRATIONS = {2: _V2_COLUMNS, 3: _V3_COLUMNS}


def _is_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _schema_statements() -> list[str]:
    statements: list[str] = []
    buf = ""
    for line in _SCHEMA_PATH.read_text(encoding="utf-8").splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    if buf.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()
    ):
        raise RuntimeError("schema.sql ends with an incomplete statement")
    return statements


def connect(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL").fetchone())
    conn.execute("PRAGMA synchronous=NORMAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        _initialize(conn)
    elif version < SCHEMA_VERSION:
        _migrate(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"unsupported schema version {version}")
    return conn


def _initialize(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] != 0:
            return
        for stmt in _schema_statements():
            conn.execute(stmt)
        _set_version(conn)


def _set_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a version 1 or 2 database in place, in one transaction.

    Every step only adds nullable or defaulted columns, so existing rows keep
    their values. The candidates status CHECK already lists every state used
    through version 3, so no table is rebuilt.
    """
    with transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if version < 1:
            raise RuntimeError(f"cannot migrate schema version {version}")
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for table, column, definition in _MIGRATIONS[step]:
                present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # New tables and indexes; existing tables are left alone by IF NOT EXISTS.
        for stmt in _schema_statements():
            conn.execute(stmt)
        _set_version(conn)


def _retry_locked(fn):
    deadline = time.monotonic() + _BEGIN_BACKOFF_BUDGET
    for attempt in range(_BEGIN_ATTEMPTS):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc) or attempt == _BEGIN_ATTEMPTS - 1:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(remaining, 0.01 + random.random() * 0.05 * (attempt + 1)))
    raise AssertionError("unreachable")


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True):
    """BEGIN (IMMEDIATE by default) ... COMMIT, or ROLLBACK on error.

    If the connection is already inside a transaction the block joins it and the
    outer owner commits.
    """
    if conn.in_transaction:
        yield conn
        return
    begin = "BEGIN IMMEDIATE" if immediate else "BEGIN"
    _retry_locked(lambda: conn.execute(begin))
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def ingest_turn_complete(
    conn: sqlite3.Connection,
    episode_row: dict,
    evidence_rows: list[dict],
    now: datetime,
) -> dict:
    from . import queue

    raw_days = episode_row.get("_raw_retention_days", DEFAULT_RAW_RETENTION_DAYS)
    row = {k: episode_row.get(k) for k in _EPISODE_COLUMNS}
    if row["captured_at"] is None:
        row["captured_at"] = iso(now)
    retain_until = iso(now + timedelta(days=raw_days))

    with transaction(conn):
        cur = conn.execute(
            f"INSERT INTO episodes ({', '.join(_EPISODE_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _EPISODE_COLUMNS)}) "
            "ON CONFLICT (session_id, turn_id) DO NOTHING",
            [row[k] for k in _EPISODE_COLUMNS],
        )
        if cur.rowcount == 0:
            existing = conn.execute(
                "SELECT episode_id FROM episodes WHERE session_id = ? AND turn_id = ?",
                (row["session_id"], row["turn_id"]),
            ).fetchone()
            episode_id = existing["episode_id"]
            job = conn.execute(
                "SELECT id FROM jobs WHERE dedup_key = ?", (f"review:{episode_id}",)
            ).fetchone()
            return {
                "episode_id": episode_id,
                "job_id": job["id"] if job else None,
                "duplicate": True,
            }

        episode_id = row["episode_id"]
        for ev in evidence_rows:
            conn.execute(
                "INSERT INTO evidence (episode_id, evidence_id, position, kind, role, content, "
                "content_sha256, provenance_json, truncated, retain_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    ev["evidence_id"],
                    ev["position"],
                    ev["kind"],
                    ev["role"],
                    ev["content"],
                    ev["content_sha256"],
                    ev["provenance_json"],
                    int(bool(ev.get("truncated", 0))),
                    retain_until,
                ),
            )
        job_id, _ = queue.enqueue(
            conn,
            "review",
            f"review:{episode_id}",
            {"episode_id": episode_id},
            now,
            episode_id=episode_id,
        )
    return {"episode_id": episode_id, "job_id": job_id, "duplicate": False}


def record_activity(conn, session_id: str, state: str, turn_id, now: datetime) -> None:
    """Record foreground activity at event time `now`.

    Events can arrive out of order (separate ingress processes), so an update
    older than the stored one is ignored, `idle` only clears the turn it
    completes, and nothing but a newer `active` replaces `ended`.
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO foreground_activity (session_id, state, turn_id, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET "
            "state = excluded.state, turn_id = excluded.turn_id, updated_at = excluded.updated_at "
            "WHERE excluded.updated_at >= foreground_activity.updated_at AND ("
            "excluded.state = 'ended' "
            "OR (excluded.state = 'active') "
            "OR (excluded.state = 'idle' AND foreground_activity.state != 'ended' AND "
            "(foreground_activity.state != 'active' OR foreground_activity.turn_id IS excluded.turn_id)))",
            (session_id, state, turn_id, iso(now)),
        )


def record_capture_error(conn, session_id, turn_id, code: str, detail: str, now: datetime) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO capture_errors (session_id, turn_id, code, detail, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, turn_id, code, (detail or "")[:2000], iso(now)),
        )
        return cur.lastrowid


def queue_counts(conn) -> dict:
    counts = {"queued": 0, "leased": 0, "retry_wait": 0, "blocked": 0, "failed": 0, "succeeded": 0}
    for r in conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
        state = r["state"]
        if state in BLOCKED_STATES:
            counts["blocked"] += r["n"]
        elif state in counts:
            counts[state] += r["n"]
    return counts


def get_meta(conn, key: str):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def set_meta(conn, key: str, value) -> None:
    if not isinstance(value, str):
        value = canonical_json(value)
    with transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
