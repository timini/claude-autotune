"""Application, activation tracking, regression handling and revert (M5).

`apply_handler` fast-forwards the user's branch to an accepted candidate
commit after rechecking, in order: the branch, HEAD against the evaluated
base, the policy digest and evaluator identity, a clean index and worktree,
known foreground activity and the pause flags. A failed recheck leaves the
candidate waiting_to_apply with the exact reason (or stale) and never touches
the checkout. Git's own ff-only merge refuses to overwrite local changes; the
result is verified afterwards and any mismatch pauses application under meta
`git_reconciliation_required`. Nothing is stashed, reset, rebased or pushed.

Recovery relies on the commit trailers: before doing anything, a commit with
`Autotune-Candidate: <id>` (or `Autotune-Revert: <id>`) reachable from the
target branch is taken as already applied and recorded, so a crash at any
point between merge and record never produces a second commit. The record is
written in its own short transaction inside the handler, followed by the
`apply_after_record` fault point, so a crash after it is also covered.

`verify_active_handler` credits a candidate only on positive evidence: an
episode at or after the applied commit that observed a changed path being
loaded. `note_review` runs inside the review commit and flags a suspected
regression; `reproduce_regression_handler` reruns the required cases against
the applied commit and its parent and reverts through an isolated worktree
when the regression reproduces.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from . import locks, queue, store
from .faults import fault_point
from .util import canonical_json, iso, parse_iso

APPLYABLE_STATES = ("accepted", "waiting_to_apply")
APPLIED_STATES = ("applied", "observed_active")
REVERTIBLE_STATES = ("applied", "observed_active", "regression_suspected")
SEVERE_DIMENSIONS = ("outcome", "instruction_adherence", "verification")

META_PAUSED = "paused"
META_APPLY_PAUSED = "apply_paused_reason"
META_RECONCILE = "git_reconciliation_required"

APPLY_LOCK_RETRY_SECONDS = 30
BRANCH_RETRY_SECONDS = 300
DIRTY_RETRY_SECONDS = 600
FOREGROUND_RETRY_SECONDS = 600
PAUSED_RETRY_SECONDS = 600
REVERT_HEAD_RETRY_SECONDS = 30
FOREGROUND_WINDOW = timedelta(hours=6)
VERIFY_RETRY_SECONDS = 86400
VERIFY_WINDOW = timedelta(days=14)

REVERT_PREFIX = "revert-"

# Error codes this module puts on a job (job last_error).
ERROR_CODES = (
    "apply_lock_busy",
    "branch_changed",
    "checkout_dirty",
    "foreground_active",
    "apply_paused",
    "git_reconciliation_required",
    "candidate_commit_missing",
    "exposure_unknown",
    "revert_conflict",
    "revert_scope_violation",
    "revert_head_moved",
    "experiment_lock_busy",
)
# Candidate reasons only; the job itself succeeds.
REASON_CODES = (
    "base_changed",
    "policy_changed",
    "reconciled",
    "exposure_unobserved",
    "same_failure_signature",
    "severe_failure",
    "manual_revert",
    "regression_not_reproduced",
    "reproduction_inconclusive",
)


def _result(status, result=None, error_code=None, detail=None, **extra):
    from . import worker

    return worker.HandlerResult(status, result, error_code, detail, **extra)


def _loads(text, default):
    if text is None:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def apply_lock_path(data_dir) -> Path:
    return Path(data_dir) / "apply.lock"


def _candidate(conn, candidate_id) -> dict | None:
    if not candidate_id:
        return None
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def _job_candidate_id(job) -> str | None:
    payload = _loads(job["payload_json"], {})
    return payload.get("candidate_id") or job["candidate_id"]


def _set(conn, candidate_id: str, now, **cols) -> None:
    cols["updated_at"] = iso(now)
    sets = ", ".join(f"{k} = ?" for k in cols)
    with store.transaction(conn):
        conn.execute(f"UPDATE candidates SET {sets} WHERE candidate_id = ?", (*cols.values(), candidate_id))


def _set_meta_in(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _flag(conn, key: str) -> str | None:
    value = store.get_meta(conn, key)
    return value if value not in (None, "") else None


def pause_reason(conn, *, for_revert: bool = False) -> str | None:
    """Why application is paused, or None. A revert ignores the regression
    pause, which exists to let it run."""
    paused = _flag(conn, META_PAUSED)
    if paused is not None and paused.strip().lower() in ("1", "true", "yes"):
        return "paused"
    reconcile = _flag(conn, META_RECONCILE)
    if reconcile is not None:
        return f"{META_RECONCILE}: {reconcile}"[:300]
    if not for_revert:
        regression = _flag(conn, META_APPLY_PAUSED)
        if regression is not None:
            return regression
    return None


def _clear_regression_pause(conn, candidate_id: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ? AND value = ?",
                 (META_APPLY_PAUSED, f"regression_suspected:{candidate_id}"))


class _Locked:
    """The per-project application lock, held without waiting."""

    def __init__(self, data_dir) -> None:
        self.lock = locks.FileLock(apply_lock_path(data_dir), blocking=False)

    def __enter__(self) -> bool:
        return self.lock.acquire()

    def __exit__(self, *exc) -> None:
        self.lock.release()


# Shared rechecks


def _target_branch(repo, cand: dict) -> str | None:
    """The branch the candidate's base came from. None (proposed on a detached
    HEAD) means no branch is a target; the checkout is never used instead."""
    return cand.get("target_branch") or None


def _foreground_active(conn, now) -> str | None:
    since = iso(now - FOREGROUND_WINDOW)
    row = conn.execute(
        "SELECT session_id, updated_at FROM foreground_activity WHERE state = 'active' AND updated_at >= ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (since,),
    ).fetchone()
    return None if row is None else f"session {row['session_id']} active since {row['updated_at']}"


def _blocking_paths(repo, added: list[str]) -> list[str]:
    """Paths a merge adding `added` would collide with: an existing file or
    symlink at the path (untracked or ignored), or a non-directory at one of
    its parent directories."""
    out = []
    for rel in added:
        full = Path(repo) / rel
        if os.path.lexists(full):
            out.append(rel)
            continue
        parts = rel.split("/")
        for i in range(1, len(parts)):
            parent = Path(repo, *parts[:i])
            if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
                out.append("/".join(parts[:i]))
                break
    return sorted(set(out))


# State files of a Git operation the user has not finished (per worktree).
IN_PROGRESS_PATHS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer",
                     "BISECT_LOG")


def _operation_in_progress(repo) -> list[str]:
    from . import git_ops

    found = []
    for name in IN_PROGRESS_PATHS:
        proc = git_ops.run_git(repo, ["rev-parse", "--path-format=absolute", "--git-path", name])
        if os.path.lexists(proc.stdout.decode("utf-8", "replace").strip()):
            found.append(name)
    return found


def _checkout_problem(repo, from_rev: str, to_rev: str) -> str | None:
    from . import git_ops

    busy = _operation_in_progress(repo)
    if busy:
        return f"a Git operation is in progress ({', '.join(busy)})"
    tracked = git_ops.tracked_changes(repo)
    if tracked:
        return f"uncommitted changes to tracked files: {', '.join(tracked[:5])}"
    added = [c.path for c in git_ops.changed_files(repo, from_rev, to_rev) if c.status == "A"]
    blocked = _blocking_paths(repo, added)
    if blocked:
        return f"untracked files at paths the change adds: {', '.join(blocked[:5])}"
    return None


def _wait(conn, cand: dict, now, code: str, detail: str, delay: float, *, status: str = "waiting_to_apply"):
    _set(conn, cand["candidate_id"], now, status=status, reason=code)
    return _result("retry", None, code, detail, retry_after_seconds=delay)


def _post_merge(conn, repo, expected: str, what: str, candidate_id: str) -> str | None:
    """Verify the checkout after a merge. On a mismatch pause application and return why."""
    from . import git_ops

    try:
        ok = git_ops.worktree_matches(repo, expected)
        detail = None if ok else f"after {what} of {candidate_id}, HEAD or files differ from {expected}"
    except git_ops.GitError as exc:
        detail = f"after {what} of {candidate_id}: {exc.detail}"
    if detail is not None:
        with store.transaction(conn):
            _set_meta_in(conn, META_RECONCILE, detail[:500])
    return detail


# Apply


def apply_handler(ctx, job):
    candidate_id = _job_candidate_id(job)
    with _Locked(ctx.data_dir) as held:
        if not held:
            return _result("retry", None, "apply_lock_busy", "another application is in progress",
                           retry_after_seconds=APPLY_LOCK_RETRY_SECONDS)
        conn = store.connect(ctx.db_path)
        try:
            return _apply(ctx, conn, candidate_id)
        finally:
            conn.close()


def _apply(ctx, conn, candidate_id):
    from . import git_ops

    cand = _candidate(conn, candidate_id)
    if cand is None:
        return _result("succeeded", {"skipped": "candidate_deleted"})
    status = cand["status"]
    if status in APPLIED_STATES or status in ("regression_suspected", "reverted"):
        return _result("succeeded", {"skipped": "already_applied", "applied_commit": cand.get("applied_commit")})
    if status not in APPLYABLE_STATES:
        return _result("succeeded", {"skipped": f"candidate_{status}"})
    commit = cand.get("candidate_commit")
    if not commit:
        return _result("failed", None, "candidate_commit_missing", f"candidate {candidate_id} has no commit")

    repo = ctx.project_dir
    now = ctx.now()
    target = _target_branch(repo, cand)

    # Reconcile: an application that reached Git but not the database.
    if target and git_ops.branch_head(repo, target):
        found = git_ops.find_trailer_commits(repo, candidate_id, refs=[f"refs/heads/{target}"])
        if found:
            _record_applied(ctx, conn, cand, found[0], reconciled=True)
            return _result("succeeded", {"applied_commit": found[0], "reconciled": True})

    current = git_ops.current_branch(repo)
    # A detached HEAD is never a target: a fast-forward there would move no branch.
    if current is None or current != target:
        return _wait(conn, cand, now, "branch_changed",
                     f"checkout is on {current or 'a detached HEAD'}, candidate targets {target or 'no branch'}",
                     BRANCH_RETRY_SECONDS)
    head = git_ops.head(repo)
    if head != cand["base_commit"]:
        return _stale(ctx, conn, cand, "base_changed", f"HEAD is {head}, candidate was evaluated on {cand['base_commit']}")
    changed = _policy_change(ctx, repo, cand)
    if changed is not None:
        return _stale(ctx, conn, cand, "policy_changed", changed)
    problem = _checkout_problem(repo, cand["base_commit"], commit)
    if problem is not None:
        return _wait(conn, cand, now, "checkout_dirty", problem, DIRTY_RETRY_SECONDS)
    active = _foreground_active(conn, now)
    if active is not None:
        return _wait(conn, cand, now, "foreground_active", active, FOREGROUND_RETRY_SECONDS)
    paused = pause_reason(conn)
    if paused is not None:
        return _wait(conn, cand, now, "apply_paused", paused, PAUSED_RETRY_SECONDS)

    fault_point("apply_before_merge")
    try:
        git_ops.merge_ff_only(repo, commit)
    except git_ops.GitError as exc:
        if git_ops.head(repo) == head:
            # Git refused, for example because a file changed after the recheck.
            return _wait(conn, cand, ctx.now(), "checkout_dirty", f"fast-forward refused: {exc.detail}",
                         DIRTY_RETRY_SECONDS)
        raise
    fault_point("apply_after_merge")

    mismatch = _post_merge(conn, repo, commit, "application", candidate_id)
    if mismatch is not None and git_ops.head(repo) != git_ops.rev_parse(repo, commit):
        _set(conn, candidate_id, ctx.now(), status="waiting_to_apply", reason=META_RECONCILE)
        return _result("failed", None, META_RECONCILE, mismatch)
    _record_applied(ctx, conn, cand, git_ops.rev_parse(repo, commit),
                    reason=META_RECONCILE if mismatch is not None else None)
    fault_point("apply_after_record")
    return _result("succeeded", {"applied_commit": commit, "verified": mismatch is None})


def _policy_change(ctx, repo, cand: dict) -> str | None:
    from . import evaluate

    now_digest = evaluate.policy_digest(repo)
    if not cand.get("policy_digest") or now_digest != cand["policy_digest"]:
        return "the project policy (.autotune.json) changed since evaluation"
    before = _loads(cand.get("evaluator_commit"), {})
    after = evaluate.evaluator_identity(ctx, repo, cand["base_commit"])
    if not before or before != after:
        return "the evaluator identity changed since evaluation"
    return None


def _stale(ctx, conn, cand: dict, code: str, detail: str):
    from . import evaluate

    removed = evaluate.remove_candidate_worktree(ctx, cand)
    cols = {"status": "stale", "reason": code}
    if removed:
        cols["worktree_path"] = None
    _set(conn, cand["candidate_id"], ctx.now(), **cols)
    return _result("succeeded", {"candidate_id": cand["candidate_id"], "status": "stale", "reason": code,
                                 "detail": detail})


def _record_applied(ctx, conn, cand: dict, applied_commit: str, *, reconciled: bool = False,
                    reason: str | None = None) -> None:
    from . import evaluate

    cid = cand["candidate_id"]
    removed = evaluate.remove_candidate_worktree(ctx, cand)
    now = ctx.now()
    with store.transaction(conn):
        sets = "status = 'applied', applied_commit = ?, applied_at = ?, reason = ?, exposure = 'unknown', updated_at = ?"
        if removed:
            sets += ", worktree_path = NULL"
        conn.execute(
            f"UPDATE candidates SET {sets} WHERE candidate_id = ? AND status IN (?, ?)",
            (applied_commit, iso(now), reason or ("reconciled" if reconciled else None), iso(now), cid,
             *APPLYABLE_STATES),
        )
        if cand.get("change_kind") == "consolidation":
            _record_merge(conn, {**cand, "applied_commit": applied_commit}, now)
        queue.enqueue(conn, "verify_active", f"verify_active:{cid}", {"candidate_id": cid}, now, candidate_id=cid)


def _record_merge(conn, cand: dict, now) -> None:
    """Link removed skill paths to the applied consolidation (skill_events
    merge rows). A missing or failing skills module never blocks application."""
    conn.execute("SAVEPOINT record_merge")
    try:
        from . import skills

        skills.record_merge(conn, cand, now)
    except Exception:
        conn.execute("ROLLBACK TO record_merge")
    conn.execute("RELEASE record_merge")


# Activation


def changed_paths(repo, cand: dict) -> list[str]:
    """Paths the candidate commit changed relative to its base."""
    from . import git_ops

    if cand.get("candidate_commit"):
        try:
            return [c.path for c in git_ops.changed_files(repo, cand["base_commit"], cand["candidate_commit"])]
        except git_ops.GitError:
            pass
    files = _loads(cand.get("validation_json"), {}).get("files")
    return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []


def _resolve(path: str, toplevel: str) -> str:
    path = path.replace("\\", "/").rstrip("/")
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(toplevel, path))


def exposes(harness: dict, changed: list[str], toplevel) -> bool:
    """True when the loaded harness observed one of the changed paths of the
    repository at `toplevel`: an instruction file_path or skill path that is
    exactly <toplevel>/<changed path>, or a skill directory under the toplevel
    that contains it. A file with the same name elsewhere (the user's
    ~/.claude, a nested directory, a user-level skill) is not exposure."""
    if not isinstance(harness, dict) or harness.get("exposure") != "observed":
        return False
    if not isinstance(toplevel, str) or not toplevel:
        return False
    loaded = [i.get("file_path") for i in harness.get("instructions") or [] if isinstance(i, dict)]
    skills = [s.get("path") for s in harness.get("skills") or [] if isinstance(s, dict)]
    loaded = {_resolve(p, toplevel) for p in loaded + skills if isinstance(p, str) and p}
    skill_dirs = {_resolve(p, toplevel) for p in skills if isinstance(p, str) and p}
    for path in changed:
        full = _resolve(path, toplevel)
        if full in loaded:
            return True
        if any(full.startswith(d + os.sep) for d in skill_dirs):
            return True
    return False


def _descends(repo, applied_commit: str, head) -> bool:
    from . import git_ops

    if not isinstance(head, str) or not head:
        return False
    if head == applied_commit:
        return True
    if not repo or not os.path.isdir(repo):
        return False
    try:
        return git_ops.is_ancestor_or_equal(repo, applied_commit, head)
    except git_ops.GitError:
        return False


def verify_active_handler(ctx, job):
    conn = store.connect(ctx.db_path)
    try:
        return _verify(ctx, conn, _job_candidate_id(job))
    finally:
        conn.close()


def _verify(ctx, conn, candidate_id):
    cand = _candidate(conn, candidate_id)
    if cand is None:
        return _result("succeeded", {"skipped": "candidate_deleted"})
    if cand["status"] != "applied":
        return _result("succeeded", {"skipped": f"candidate_{cand['status']}", "exposure": cand.get("exposure")})
    applied, applied_at = cand.get("applied_commit"), cand.get("applied_at")
    repo = ctx.project_dir
    changed = changed_paths(repo, cand)
    rows = conn.execute(
        "SELECT episode_id, completed_at, repository_json, loaded_harness_json FROM episodes "
        "WHERE completed_at > ? ORDER BY completed_at",
        (applied_at or "",),
    ).fetchall()
    for r in rows:
        repository = _loads(r["repository_json"], {})
        head = repository.get("head")
        if not _descends(repo, applied, head):
            continue
        if not exposes(_loads(r["loaded_harness_json"], {}), changed, repository.get("toplevel") or repo):
            continue
        episode_id = r["episode_id"]

        def commit(c, episode_id=episode_id) -> None:
            c.execute(
                "UPDATE candidates SET status = 'observed_active', exposure = 'observed', reason = NULL, "
                "updated_at = ? WHERE candidate_id = ? AND status = 'applied'",
                (iso(ctx.now()), candidate_id),
            )

        return _result("succeeded", {"exposure": "observed", "episode_id": episode_id}, commit=commit)

    now = ctx.now()
    if applied_at and now - parse_iso(applied_at) >= VERIFY_WINDOW:
        def give_up(c) -> None:
            c.execute(
                "UPDATE candidates SET reason = 'exposure_unobserved', exposure = 'unknown', updated_at = ? "
                "WHERE candidate_id = ? AND status = 'applied'",
                (iso(ctx.now()), candidate_id),
            )

        return _result("succeeded", {"exposure": "unknown", "reason": "exposure_unobserved"}, commit=give_up)
    _set(conn, candidate_id, now, exposure="unknown")
    return _result("retry", {"exposure": "unknown"}, "exposure_unknown",
                   canonical_json({"exposure": "unknown"}), retry_after_seconds=VERIFY_RETRY_SECONDS)


# Regression detection (runs inside the review commit transaction)


def note_review(conn, review_row, now) -> list[str]:
    """Flag applied candidates that a new valid review implicates.

    Runs inside the caller's transaction and never raises: a failure is rolled
    back to a savepoint so the review itself is still stored. Returns the ids
    of candidates moved to regression_suspected.
    """
    conn.execute("SAVEPOINT autotune_note_review")
    try:
        flagged = _note_review(conn, dict(review_row), now)
    except Exception:
        conn.execute("ROLLBACK TO autotune_note_review")
        flagged = []
    conn.execute("RELEASE autotune_note_review")
    return flagged


def _severe(review: dict) -> bool:
    dims = review.get("dimensions") or {}
    for name in SEVERE_DIMENSIONS:
        d = dims.get(name) or {}
        if type(d.get("score")) is int and d["score"] == 0 and d.get("evidence_ids"):
            return True
    return False


def _linked(review: dict, harness: dict, files: list[str], toplevel) -> bool:
    affected = [p.strip().removeprefix("./") for p in review.get("affected_paths") or [] if isinstance(p, str)]
    return bool(set(affected) & set(files)) or exposes(harness, files, toplevel)


def _note_review(conn, row: dict, now) -> list[str]:
    if (row.get("status") or "valid") != "valid":
        return []
    review = _loads(row.get("review_json"), {})
    ep = conn.execute("SELECT repository_json, loaded_harness_json FROM episodes WHERE episode_id = ?",
                      (row.get("episode_id"),)).fetchone()
    if ep is None:
        return []
    repository = _loads(ep["repository_json"], {})
    harness = _loads(ep["loaded_harness_json"], {})
    head = repository.get("head")
    repo = repository.get("toplevel")
    signature = row.get("failure_signature") or review.get("failure_signature")
    severe = _severe(review)
    marks = ", ".join("?" for _ in APPLIED_STATES)
    cands = conn.execute(
        f"SELECT * FROM candidates WHERE status IN ({marks}) AND applied_commit IS NOT NULL ORDER BY applied_at",
        APPLIED_STATES,
    ).fetchall()
    flagged = []
    for c in cands:
        c = dict(c)
        if not _descends(repo, c["applied_commit"], head):
            continue
        files = _loads(c.get("validation_json"), {}).get("files") or []
        if signature and signature == c.get("failure_signature"):
            reason = "same_failure_signature"
        elif severe and _linked(review, harness, files, repo):
            reason = "severe_failure"
        else:
            continue
        cid = c["candidate_id"]
        conn.execute(
            "UPDATE candidates SET status = 'regression_suspected', reason = ?, updated_at = ? WHERE candidate_id = ?",
            (reason, iso(now), cid),
        )
        _set_meta_in(conn, META_APPLY_PAUSED, f"regression_suspected:{cid}")
        queue.enqueue(conn, "reproduce_regression", f"reproduce_regression:{cid}:{row.get('review_id')}",
                      {"candidate_id": cid, "review_id": row.get("review_id"), "mode": "reproduce"},
                      now, candidate_id=cid)
        flagged.append(cid)
    return flagged


# Reproduction and revert


def reproduce_regression_handler(ctx, job):
    payload = _loads(job["payload_json"], {})
    candidate_id = payload.get("candidate_id") or job["candidate_id"]
    conn = store.connect(ctx.db_path)
    try:
        cand = _candidate(conn, candidate_id)
        if cand is None:
            return _result("succeeded", {"skipped": "candidate_deleted"})
        if payload.get("mode") == "revert_now":
            if cand["status"] not in REVERTIBLE_STATES:
                return _result("succeeded", {"skipped": f"candidate_{cand['status']}"})
            if cand["status"] != "regression_suspected":
                with store.transaction(conn):
                    conn.execute("UPDATE candidates SET status = 'regression_suspected', reason = 'manual_revert', "
                                 "updated_at = ? WHERE candidate_id = ?", (iso(ctx.now()), candidate_id))
                    _set_meta_in(conn, META_APPLY_PAUSED, f"regression_suspected:{candidate_id}")
                cand = _candidate(conn, candidate_id)
            return revert(ctx, conn, cand)
        if cand["status"] != "regression_suspected":
            return _result("succeeded", {"skipped": f"candidate_{cand['status']}"})
        with ctx.experiment_lock() as held:
            if not held:
                return _result("retry", None, "experiment_lock_busy", "another project is running an experiment",
                               retry_after_seconds=120)
            return _reproduce(ctx, job, conn, cand, payload)
    finally:
        conn.close()


def _reproduce(ctx, job, conn, cand: dict, payload: dict):
    from . import cases as cases_mod
    from . import claude_cli, evaluate, gate, git_ops

    repo = ctx.project_dir
    cid = cand["candidate_id"]
    applied = cand["applied_commit"]
    parent = git_ops.parent_of(repo, applied)
    loaded, _errors = cases_mod.load_cases(repo, cand["base_commit"])
    by_id = {c.id: c for c in loaded}
    ids = _loads(cand.get("required_cases_json"), [])
    required = [by_id[i] for i in ids if i in by_id]
    first = payload.get("first_attempt")
    if not isinstance(first, int):
        row = conn.execute("SELECT MAX(attempt) FROM evaluation_runs WHERE candidate_id = ?", (cid,)).fetchone()
        first = int(row[0] or 0) + 1
        payload = dict(payload, first_attempt=first)
        with store.transaction(conn):
            conn.execute("UPDATE jobs SET payload_json = ? WHERE id = ?", (canonical_json(payload), job["id"]))
    if not required or len(required) != len(ids):
        _set(conn, cid, ctx.now(), reason="reproduction_inconclusive")
        return _result("succeeded", {"candidate_id": cid, "reproduced": None, "reason": "reproduction_inconclusive",
                                     "detail": "required cases are not all available"})

    exp = evaluate.Experiment(candidate_id=cid, cases=required, baseline_commit=parent, candidate_commit=applied,
                              fixture_commit=parent, first_attempt=first)
    stop = evaluate.run_experiment(ctx, claude_cli.adapter_for(ctx), conn, exp)
    if stop is not None:
        return stop
    identity = evaluate.evaluator_identity(ctx, repo, cand["base_commit"])
    policy = evaluate.policy_digest(repo)
    pseudo = dict(cand, base_commit=parent, candidate_commit=applied)
    decision = gate.decide(evaluate.gate_inputs(ctx, conn, pseudo, exp, evaluator_before=identity,
                                                evaluator_after=identity, policy_start=policy, policy_now=policy))
    reproduced = _reproduced(decision, exp, conn)
    if reproduced is True:
        result = revert(ctx, conn, _candidate(conn, cid))
        if result.result is not None:
            result.result = dict(result.result, reproduction=decision)
        return result
    if reproduced is None:
        _set(conn, cid, ctx.now(), reason="reproduction_inconclusive")
        return _result("succeeded", {"candidate_id": cid, "reproduced": None, "reason": "reproduction_inconclusive",
                                     "reproduction": decision})
    status = "observed_active" if cand.get("exposure") == "observed" else "applied"
    with store.transaction(conn):
        conn.execute("UPDATE candidates SET status = ?, reason = 'regression_not_reproduced', updated_at = ? "
                     "WHERE candidate_id = ? AND status = 'regression_suspected'", (status, iso(ctx.now()), cid))
        _clear_regression_pause(conn, cid)
    return _result("succeeded", {"candidate_id": cid, "reproduced": False, "status": status,
                                 "reproduction": decision})


def _reproduced(decision: dict, exp, conn) -> bool | None:
    """True when the applied commit fails where its parent did not (a critical
    or known-success failure) or a target case still fails on every trial; False
    when every result is complete and none of that holds; None otherwise."""
    from . import evaluate

    checks = decision.get("checks") or {}
    # A known-success failure counts whether reproducible or stochastic: an
    # ambiguous confirmation must not leave the change applied.
    if checks.get("critical") == "fail" or checks.get("known_successes") == "fail":
        return True
    slots = evaluate._slot_rows(evaluate.phase_rows(conn, exp.candidate_id, exp.first_attempt))
    for case in exp.cases:
        if case.kind != "target":
            continue
        runs = [evaluate._effective(slots.get((case.id, "candidate", t), []))
                for t in range(1, evaluate._trials(case) + 1)]
        if runs and all(r is not None and r["outcome"] in ("task_failure", "constraint_violation") for r in runs):
            return True
    if checks.get("completeness") != "pass" or checks.get("integrity") != "pass":
        return None
    return False


def _revert_branch(candidate_id: str) -> str:
    return f"autotune/{REVERT_PREFIX}{candidate_id}"


def _revert_worktree(ctx, candidate_id: str) -> Path:
    return Path(ctx.data_dir) / "worktrees" / f"{REVERT_PREFIX}{candidate_id}"


def _drop_revert(ctx, candidate_id: str) -> None:
    from . import git_ops

    try:
        git_ops.remove_worktree(ctx.project_dir, _revert_worktree(ctx, candidate_id),
                                delete_branch=_revert_branch(candidate_id),
                                worktrees_root=Path(ctx.data_dir) / "worktrees")
    except (git_ops.GitError, ValueError, OSError):
        pass


def revert(ctx, conn, cand: dict):
    """Revert an applied candidate through an isolated branch, then fast-forward
    the user's branch to it with the same rechecks as application."""
    with _Locked(ctx.data_dir) as held:
        if not held:
            return _result("retry", None, "apply_lock_busy", "another application is in progress",
                           retry_after_seconds=APPLY_LOCK_RETRY_SECONDS)
        return _revert(ctx, conn, cand)


def _revert(ctx, conn, cand: dict):
    from . import git_ops

    repo = ctx.project_dir
    cid = cand["candidate_id"]
    applied = cand["applied_commit"]
    target = _target_branch(repo, cand)
    branch = _revert_branch(cid)

    if target and git_ops.branch_head(repo, target):
        found = git_ops.find_trailer_commits_for(repo, git_ops.REVERT_TRAILER_KEY, cid,
                                                 refs=[f"refs/heads/{target}"])
        if found:
            _record_reverted(ctx, conn, cid, found[0])
            return _result("succeeded", {"reverted_commit": found[0], "reconciled": True})

    now = ctx.now()
    current = git_ops.current_branch(repo)
    # A detached HEAD is never a target: a fast-forward there would move no branch.
    if current is None or current != target:
        return _wait(conn, cand, now, "branch_changed",
                     f"checkout is on {current or 'a detached HEAD'}, candidate was applied to {target or 'no branch'}",
                     BRANCH_RETRY_SECONDS, status="regression_suspected")
    paused = pause_reason(conn, for_revert=True)
    if paused is not None:
        return _wait(conn, cand, now, "apply_paused", paused, PAUSED_RETRY_SECONDS, status="regression_suspected")
    head = git_ops.head(repo)
    if not git_ops.is_ancestor_or_equal(repo, applied, head):
        detail = f"applied commit {applied} of {cid} is not in the history of HEAD {head}"
        with store.transaction(conn):
            _set_meta_in(conn, META_RECONCILE, detail)
        _set(conn, cid, now, reason=META_RECONCILE)
        return _result("failed", None, META_RECONCILE, detail)

    revert_commit = _prepared_revert(repo, branch, head, cid)
    if revert_commit is None:
        _drop_revert(ctx, cid)
        wt = _revert_worktree(ctx, cid)
        wt.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(wt.parent, 0o700)
        git_ops.create_worktree(repo, branch, head, str(wt))
        try:
            revert_commit = git_ops.revert_in_worktree(repo, wt, applied, cid)
        except git_ops.RevertConflict as exc:
            _drop_revert(ctx, cid)
            _set(conn, cid, ctx.now(), reason="revert_conflict")
            return _result("failed", None, "revert_conflict", f"git revert of {applied} conflicts: {exc.detail}")
        fault_point("revert_after_commit")
    violation = _revert_scope(repo, cand, head, revert_commit)
    if violation is not None:
        _drop_revert(ctx, cid)
        _set(conn, cid, ctx.now(), reason="revert_scope_violation")
        return _result("failed", None, "revert_scope_violation", violation)

    problem = _checkout_problem(repo, head, revert_commit)
    if problem is not None:
        return _wait(conn, cand, ctx.now(), "checkout_dirty", problem, DIRTY_RETRY_SECONDS,
                     status="regression_suspected")
    active = _foreground_active(conn, ctx.now())
    if active is not None:
        return _wait(conn, cand, ctx.now(), "foreground_active", active, FOREGROUND_RETRY_SECONDS,
                     status="regression_suspected")
    if git_ops.head(repo) != git_ops.parent_of(repo, revert_commit):
        _drop_revert(ctx, cid)
        return _wait(conn, cand, ctx.now(), "revert_head_moved", "HEAD moved while the revert was prepared",
                     REVERT_HEAD_RETRY_SECONDS, status="regression_suspected")
    try:
        git_ops.merge_ff_only(repo, revert_commit)
    except git_ops.GitError as exc:
        if git_ops.head(repo) == head:
            return _wait(conn, cand, ctx.now(), "checkout_dirty", f"fast-forward refused: {exc.detail}",
                         DIRTY_RETRY_SECONDS, status="regression_suspected")
        raise
    fault_point("revert_after_merge")
    mismatch = _post_merge(conn, repo, revert_commit, "revert", cid)
    if mismatch is not None and git_ops.head(repo) != revert_commit:
        _set(conn, cid, ctx.now(), reason=META_RECONCILE)
        return _result("failed", None, META_RECONCILE, mismatch)
    _record_reverted(ctx, conn, cid, revert_commit)
    return _result("succeeded", {"reverted_commit": revert_commit, "verified": mismatch is None})


def _prepared_revert(repo, branch: str, head: str, cid: str) -> str | None:
    """A revert commit left by an earlier attempt, usable when its parent is still HEAD."""
    from . import git_ops

    tip = git_ops.branch_head(repo, branch)
    if tip is None:
        return None
    found = git_ops.find_trailer_commits_for(repo, git_ops.REVERT_TRAILER_KEY, cid, refs=[f"refs/heads/{branch}"])
    if tip in found and git_ops.parent_of(repo, tip) == head:
        return tip
    return None


def _revert_scope(repo, cand: dict, head: str, revert_commit: str) -> str | None:
    from . import git_ops

    own = set(changed_paths(repo, cand))
    touched = [c.path for c in git_ops.changed_files(repo, head, revert_commit)]
    outside = sorted(p for p in touched if p not in own)
    if outside:
        return f"revert touches paths the candidate did not change: {', '.join(outside[:5])}"
    return None


def _record_reverted(ctx, conn, cid: str, revert_commit: str) -> None:
    now = ctx.now()
    with store.transaction(conn):
        conn.execute(
            "UPDATE candidates SET status = 'reverted', reverted_commit = ?, reverted_at = ?, reason = NULL, "
            "updated_at = ? WHERE candidate_id = ? AND status IN (?, ?, ?)",
            (revert_commit, iso(now), iso(now), cid, *REVERTIBLE_STATES),
        )
        _clear_regression_pause(conn, cid)
    _drop_revert(ctx, cid)


def _register() -> None:
    from . import worker

    worker.register_handler("apply", apply_handler)
    worker.register_handler("verify_active", verify_active_handler)
    worker.register_handler("reproduce_regression", reproduce_regression_handler)


_register()
