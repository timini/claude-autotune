"""Evidence expiry, candidate branch expiry and explicit deletion."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .store import BLOCKED_STATES, transaction
from .util import iso, utc_now

_PENDING_REVIEW_STATES = ("queued", "retry_wait") + BLOCKED_STATES
_TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def evidence_expired(retain_until: str | None, captured_at: str | None, now: datetime,
                     raw_days: int | None) -> bool:
    """Whether evidence has expired under the retention policy, whether or
    not apply_retention has run yet: its retain_until has passed, or its
    episode was captured raw_days or more ago. The same rule as the UPDATE
    in apply_retention."""
    if retain_until is not None and retain_until <= iso(now):
        return True
    return raw_days is not None and captured_at is not None and captured_at <= iso(now - timedelta(days=raw_days))


def apply_retention(conn, now: datetime, raw_days: int, result_days: int, *, repo=None) -> dict:
    """Expire evidence content and stale detail; rows are kept.

    Evidence expires at its recorded retain_until, or earlier when the episode
    is older than raw_days (so a shortened setting takes effect); see
    evidence_expired, which export uses to refuse evidence retention has not
    reached yet. Unreviewed
    episodes whose evidence is all gone get an explicit evidence_expired
    outcome. Terminal job results and capture errors older than result_days are
    cleared.
    """
    ts = iso(now)
    raw_cutoff = iso(now - timedelta(days=raw_days))
    result_cutoff = iso(now - timedelta(days=result_days))
    with transaction(conn):
        expired = conn.execute(
            "UPDATE evidence SET content = NULL, expired_at = ? "
            "WHERE expired_at IS NULL AND (retain_until <= ? OR episode_id IN "
            "(SELECT episode_id FROM episodes WHERE captured_at <= ?))",
            (ts, ts, raw_cutoff),
        ).rowcount

        rows = conn.execute(
            "SELECT e.episode_id, j.id AS job_id FROM episodes e "
            "JOIN jobs j ON j.dedup_key = 'review:' || e.episode_id "
            f"WHERE e.review_status = 'pending' AND j.state IN ({_placeholders(_PENDING_REVIEW_STATES)}) "
            "AND EXISTS (SELECT 1 FROM evidence v WHERE v.episode_id = e.episode_id) "
            "AND NOT EXISTS (SELECT 1 FROM evidence v WHERE v.episode_id = e.episode_id "
            "AND v.expired_at IS NULL)",
            _PENDING_REVIEW_STATES,
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE jobs SET state = 'failed', last_error = 'evidence_expired', "
                "last_error_detail = NULL, updated_at = ? WHERE id = ?",
                (ts, r["job_id"]),
            )
            conn.execute(
                "UPDATE episodes SET review_status = 'evidence_expired' WHERE episode_id = ?",
                (r["episode_id"],),
            )

        results_cleared = conn.execute(
            f"UPDATE jobs SET result_json = NULL, last_error_detail = NULL "
            f"WHERE state IN ({_placeholders(_TERMINAL_JOB_STATES)}) AND updated_at <= ? "
            "AND (result_json IS NOT NULL OR last_error_detail IS NOT NULL)",
            (*_TERMINAL_JOB_STATES, result_cutoff),
        ).rowcount
        errors_deleted = conn.execute(
            "DELETE FROM capture_errors WHERE occurred_at <= ?", (result_cutoff,)
        ).rowcount
    out = {
        "evidence_expired": expired,
        "episodes_evidence_expired": len(rows),
        "job_results_cleared": results_cleared,
        "capture_errors_deleted": errors_deleted,
    }
    found, worktrees_root = _repo_from_db(conn)
    if repo is None:
        repo = found
    elif worktrees_root is None:
        worktrees_root = _worktrees_root(conn)
    if repo is not None:
        out.update(delete_candidate_branches(conn, repo, now, result_days, worktrees_root=worktrees_root))
    return out


# Candidate branches

BRANCH_EXPIRY_STATES = ("rejected", "inconclusive")


def _worktrees_root(conn) -> Path | None:
    row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
    return Path(row[2]).parent / "worktrees" if row is not None and row[2] else None


def _repo_from_db(conn) -> tuple[str | None, Path | None]:
    """The project's checkout (from project.json next to the database file)
    and the data dir's worktrees root."""
    try:
        row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
        if row is None or not row[2]:
            return None, None
        data_dir = Path(row[2]).parent
        meta = json.loads((data_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    top = meta.get("toplevel") if isinstance(meta, dict) else None
    if not isinstance(top, str) or not os.path.isdir(top):
        return None, None
    return top, data_dir / "worktrees"


def made_by_autotune(repo, branch: str, cand: dict) -> bool:
    """True when the branch tip is a commit Autotune made or recorded for this
    candidate, so deleting the branch cannot lose someone else's commits."""
    from . import git_ops

    tip = git_ops.branch_head(repo, branch)
    if tip is None:
        return False
    known = {cand.get(k) for k in ("candidate_commit", "base_commit", "reverted_commit")} - {None}
    if tip in known:
        return True
    proc = git_ops.run_git(repo, ["log", "-1", "--format=%ae%x00%ce", tip], check=False)
    if proc.returncode != 0:
        return False
    emails = proc.stdout.decode("utf-8", "replace").strip().split("\0")
    return all(e == git_ops.AUTHOR_EMAIL for e in emails)


def delete_branch(repo, branch: str, cand: dict) -> str:
    """Delete one Autotune branch. Returns "deleted", "missing", or why it was kept."""
    from . import git_ops

    if not isinstance(branch, str) or not branch.startswith(git_ops.BRANCH_PREFIX):
        return "kept_not_autotune"
    if not git_ops.branch_exists(repo, branch):
        return "missing"
    if not made_by_autotune(repo, branch, cand):
        return "kept_foreign_commits"
    proc = git_ops.run_git(repo, ["branch", "-D", "--end-of-options", branch], hooks_off=True, check=False)
    # A branch checked out in some worktree cannot be deleted; leave it.
    return "deleted" if proc.returncode == 0 else "kept_in_use"


def delete_candidate_branches(conn, repo, now: datetime, result_days: int, *, worktrees_root=None) -> dict:
    """Delete branches of rejected/inconclusive candidates last updated more than
    result_days ago and record branch_deleted_at. The candidate rows (decision
    records, signatures, case links) are kept, as are branches of every other
    status. A branch whose tip holds commits Autotune did not make is kept."""
    from . import git_ops

    cutoff = iso(now - timedelta(days=result_days))
    rows = conn.execute(
        f"SELECT * FROM candidates WHERE status IN ({_placeholders(BRANCH_EXPIRY_STATES)}) "
        "AND branch IS NOT NULL AND branch_deleted_at IS NULL AND updated_at <= ? ORDER BY updated_at",
        (*BRANCH_EXPIRY_STATES, cutoff),
    ).fetchall()
    counts = {"branches_deleted": 0, "branches_kept": 0}
    for r in rows:
        cand = dict(r)
        try:
            if cand.get("worktree_path") and worktrees_root is not None:
                git_ops.remove_worktree(repo, cand["worktree_path"], delete_branch=None,
                                        worktrees_root=worktrees_root)
            outcome = delete_branch(repo, cand["branch"], cand)
        except (git_ops.GitError, ValueError, OSError):
            outcome = "kept_error"
        if outcome not in ("deleted", "missing"):
            counts["branches_kept"] += 1
            continue
        with transaction(conn):
            conn.execute(
                "UPDATE candidates SET branch_deleted_at = ? WHERE candidate_id = ? AND branch_deleted_at IS NULL",
                (iso(now), cand["candidate_id"]),
            )
        counts["branches_deleted"] += outcome == "deleted"
    return counts


def purge(conn, scope: str, before: datetime | None = None, *, now: datetime | None = None) -> dict:
    """Explicit deletion requested by the user.

    "evidence" clears evidence content (rows stay, marked expired, so reviews
    citing them show as unverifiable). "all" deletes episodes with their
    evidence, reviews, skill events and jobs, plus episode-less jobs, capture
    errors and foreground activity. With `before`, only records older than it.
    Candidates, evaluation runs and meta are not touched.
    """
    if scope not in ("evidence", "all"):
        raise ValueError(f"unknown purge scope {scope!r}")
    cut = iso(before) if before is not None else None
    counts: dict = {}
    with transaction(conn):
        if scope == "evidence":
            sql = "UPDATE evidence SET content = NULL, expired_at = COALESCE(expired_at, ?) WHERE content IS NOT NULL"
            args: list = [iso(now or utc_now())]
            if cut is not None:
                sql += " AND episode_id IN (SELECT episode_id FROM episodes WHERE captured_at < ?)"
                args.append(cut)
            counts["evidence"] = conn.execute(sql, args).rowcount
            return counts

        ep_filter = "" if cut is None else " WHERE captured_at < ?"
        ep_args = [] if cut is None else [cut]
        sub = f"SELECT episode_id FROM episodes{ep_filter}"
        counts["evidence"] = conn.execute(
            f"DELETE FROM evidence WHERE episode_id IN ({sub})", ep_args
        ).rowcount
        counts["reviews"] = conn.execute(
            f"DELETE FROM reviews WHERE episode_id IN ({sub})", ep_args
        ).rowcount
        counts["skill_events"] = conn.execute(
            f"DELETE FROM skill_events WHERE episode_id IN ({sub})", ep_args
        ).rowcount
        job_sql = f"DELETE FROM jobs WHERE episode_id IN ({sub})"
        job_args = list(ep_args)
        if cut is None:
            job_sql += " OR episode_id IS NULL"
        else:
            job_sql += " OR (episode_id IS NULL AND created_at < ?)"
            job_args.append(cut)
        counts["jobs"] = conn.execute(job_sql, job_args).rowcount
        counts["episodes"] = conn.execute(f"DELETE FROM episodes{ep_filter}", ep_args).rowcount
        if cut is None:
            counts["capture_errors"] = conn.execute("DELETE FROM capture_errors").rowcount
            counts["foreground_activity"] = conn.execute("DELETE FROM foreground_activity").rowcount
        else:
            counts["capture_errors"] = conn.execute(
                "DELETE FROM capture_errors WHERE occurred_at < ?", (cut,)
            ).rowcount
            counts["foreground_activity"] = conn.execute(
                "DELETE FROM foreground_activity WHERE updated_at < ?", (cut,)
            ).rowcount
    return counts



ENFORCE_BATCH = 200
ENFORCE_ROUNDS = 5


def live_bytes(conn) -> int:
    """Bytes in use by database pages, excluding the freelist."""
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return (page_count - freelist) * page_size


def _compact(conn) -> bool:
    """VACUUM and truncate the WAL so the files shrink. Needs free space for a copy."""
    if conn.in_transaction:
        return False
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return True
    except Exception:
        return False


def enforce_cap(conn, target_bytes: int, now: datetime) -> dict:
    """Expire the oldest unexpired evidence until live page bytes are under target_bytes.

    Clearing content mostly frees space inside pages rather than whole pages,
    so live page bytes do not fall as rows are cleared. Each round therefore
    estimates the excess, clears roughly that many content bytes oldest first
    (by retain_until, then episode completed_at) in batches, then compacts the
    database and measures again. Stops when under target, when nothing is
    left to expire, or after a bounded number of rounds.
    """
    ts = iso(now)
    expired = 0
    compacted = 0
    compact_failed = False
    for _ in range(ENFORCE_ROUNDS):
        excess = live_bytes(conn) - target_bytes
        if excess < 0:
            break
        cleared_bytes = 0
        cleared_rows = 0
        while cleared_bytes <= excess:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT v.rowid AS rid, COALESCE(length(CAST(v.content AS BLOB)), 0) AS n "
                    "FROM evidence v JOIN episodes e ON e.episode_id = v.episode_id "
                    "WHERE v.expired_at IS NULL "
                    "ORDER BY v.retain_until, e.completed_at, v.episode_id, v.position LIMIT ?",
                    (ENFORCE_BATCH,),
                ).fetchall()
                if not rows:
                    break
                take = []
                for r in rows:
                    take.append(r["rid"])
                    cleared_bytes += r["n"]
                    if cleared_bytes > excess:
                        break
                conn.execute(
                    f"UPDATE evidence SET content = NULL, expired_at = ? "
                    f"WHERE rowid IN ({_placeholders(take)})",
                    (ts, *take),
                )
            cleared_rows += len(take)
        expired += cleared_rows
        if _compact(conn):
            compacted += 1
        else:
            compact_failed = True
            break
        if cleared_rows == 0:
            break
    live = live_bytes(conn)
    return {
        "evidence_expired": expired,
        "compactions": compacted,
        "compact_failed": compact_failed,
        "live_bytes": live,
        "under_target": live < target_bytes,
    }
