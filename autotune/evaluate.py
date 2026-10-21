"""Evaluate one candidate with fresh paired replays and the promotion gate (M4).

The evaluate job preregisters the required cases, the evaluator identity and
the policy digest before any run, then runs every (case, trial) pair in both
arms, in an order drawn from a seed of (candidate, case, trial). Each run is
stored in its own short transaction, so a crashed attempt resumes with the
runs it already has and never samples a pair again beyond the declared retry
(one more attempt after an infrastructure error). The gate decides from the
stored rows only. Nothing here writes to the user's checkout.

`run_experiment` is shared with regression reproduction (apply.py), which
reruns the same cases with other harness commits and later attempt numbers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import budget, config, queue, store
from .util import canonical_json, iso, new_id

EVALUABLE_STATES = ("validating", "evaluating")
EXPERIMENT_LOCK_RETRY_SECONDS = 120
MAX_RUN_ATTEMPTS = 2
JUDGE_CALLS_PER_CASE = 2
ANSWER_CHARS = 20000
ARMS = ("baseline", "candidate")
# Graders that read only the workspace, so they still apply after a timeout.
TIMEOUT_GRADER_TYPES = ("file_unchanged", "file_contains", "file_absent")

# Adapter outcomes that stop the job instead of producing a run row.
STOP_STATUS = {
    "cancelled": "cancelled",
    "auth_required": "blocked_auth",
    "budget_exhausted": "blocked_budget",
    "usage_limit": "blocked_budget",
    "cli_missing": "blocked_environment",
    "cli_incompatible": "blocked_environment",
}

# Error codes this module puts on a job (job last_error), besides STOP_STATUS keys.
ERROR_CODES = ("experiment_lock_busy", "candidate_commit_missing")
# Candidate reasons set without a gate run; gate.REASON_CODES covers the rest.
REASON_CODES = ("no_required_cases", "invalid_cases", "budget_exhausted", "cancelled")


def missing_required(cand, required) -> str | None:
    """Why the required cases cannot measure this candidate, or None. A
    consolidation is measured on the cases that touch its skills, which must
    include a negative_trigger case (cases.select_consolidation); it needs no
    target case. Every other change kind needs a target case."""
    if cand["change_kind"] == "consolidation":
        if not any(c.kind == "negative_trigger" for c in required):
            return "no negative_trigger case is required for this consolidation"
        return None
    if not any(c.kind == "target" for c in required):
        return "no target case is required for this candidate"
    return None


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


def _json(text, default):
    """Parsed JSON of any type, or `default` when absent or invalid."""
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# Identities


def policy_digest(repo) -> str | None:
    """Digest of the project's current `.autotune.json` (None when it cannot be read)."""
    try:
        cfg = config.load_config(repo)
    except (config.ConfigError, OSError):
        return None
    return None if cfg is None else config.config_digest(cfg)


def evaluator_identity(ctx, repo, base_commit: str) -> dict:
    from . import cases

    return cases.evaluator_identity(repo, base_commit, plugin_root=getattr(ctx, "plugin_root", None))


def arm_order(candidate_id: str, case_id: str, trial: int) -> tuple[str, str]:
    """Display and execution order of the two arms for one (case, trial)."""
    digest = hashlib.sha256(f"{candidate_id}\0{case_id}\0{trial}".encode("utf-8")).digest()
    return ARMS if digest[0] % 2 == 0 else (ARMS[1], ARMS[0])


def harness_globs(cfg: dict) -> list[str]:
    """Editable globs plus field-policy files: every path a candidate may change."""
    globs = list(cfg.get("editable_paths") or [])
    globs += [f for f in sorted((cfg.get("field_rules") or {}).keys()) if f not in globs]
    return globs


# Budget


def experiment_calls_left(conn, cfg: dict, now) -> int:
    start, end = budget.day_bounds(now)
    used = budget._count(conn, budget.GROUPS["experiment"], start, end)
    return budget.limit_for("experiment", cfg) - used


# Run rows


def phase_rows(conn, candidate_id: str, first_attempt: int = 1) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM evaluation_runs WHERE candidate_id = ? AND attempt >= ? "
        "ORDER BY case_id, trial, arm, attempt",
        (candidate_id, first_attempt),
    ).fetchall()
    return [dict(r) for r in rows]


def gate_run(row: dict) -> dict:
    """An evaluation_runs row in the shape gate.decide reads."""
    return {
        "run_id": row["run_id"],
        "case_id": row["case_id"],
        "arm": row["arm"],
        "trial": row["trial"],
        "attempt": row["attempt"],
        "outcome": row["outcome"],
        "checks": _json(row.get("checks_json"), []),
        "model": row.get("model"),
        "cli_build": row.get("cli_build"),
        "environment": _loads(row.get("environment_json"), {}),
        "usage": _loads(row.get("usage_json"), {}) or None,
        "duration_ms": row.get("duration_ms"),
        "fixture_digest": row.get("fixture_digest"),
        "harness_commit": row.get("harness_commit"),
        "error_code": row.get("error_code"),
    }


def _slot_rows(rows: list[dict]) -> dict[tuple, list[dict]]:
    slots: dict[tuple, list[dict]] = {}
    for r in rows:
        slots.setdefault((r["case_id"], r["arm"], r["trial"]), []).append(r)
    for v in slots.values():
        v.sort(key=lambda r: r["attempt"])
    return slots


def _slot_done(rows: list[dict]) -> bool:
    return bool(rows) and (rows[-1]["outcome"] != "infrastructure_error" or len(rows) >= MAX_RUN_ATTEMPTS)


def _effective(rows: list[dict]) -> dict | None:
    """The attempt that counts: the first one that is not an infrastructure error."""
    for r in rows[:MAX_RUN_ATTEMPTS]:
        if r["outcome"] != "infrastructure_error":
            return r
    return rows[min(len(rows), MAX_RUN_ATTEMPTS) - 1] if rows else None


def _trials(case) -> int:
    n = _get(case, "trials_per_arm") or 1
    return n if isinstance(n, int) and n > 0 else 1


def calls_needed(cases, rows: list[dict], judged: set[str]) -> int:
    """Model calls still needed: one per pair arm without a final run, two per
    judged case without a stored verdict. Retries are not counted in advance."""
    slots = _slot_rows(rows)
    n = 0
    for case in cases:
        for trial in range(1, _trials(case) + 1):
            for arm in ARMS:
                if not _slot_done(slots.get((case.id, arm, trial), [])):
                    n += 1
        if _get(case, "judge") and case.id not in judged:
            n += JUDGE_CALLS_PER_CASE
    return n


@dataclass
class Experiment:
    """One set of paired runs: which commits the arms use and where attempts start."""

    candidate_id: str
    cases: list
    baseline_commit: str
    candidate_commit: str
    fixture_commit: str
    first_attempt: int = 1
    model: str | None = None
    judge_results: dict = field(default_factory=dict)


def _insert_run(conn, row: dict) -> None:
    cols = list(row)
    with store.transaction(conn):
        conn.execute(
            f"INSERT INTO evaluation_runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )


def _frozen_model(cfg: dict, rows: list[dict]) -> str | None:
    if cfg.get("replay_model"):
        return cfg["replay_model"]
    for r in sorted(rows, key=lambda r: (r["started_at"], r["run_id"])):
        if r.get("model"):
            return r["model"]
    return None


def _run_one(ctx, adapter, conn, exp: Experiment, case, arm: str, trial: int, attempt: int,
             display: tuple[str, int]):
    """Materialize, run, grade and store one run. Returns (row or None, stop code or None)."""
    from . import git_ops, graders, replay

    cfg = ctx.config
    run_id = new_id("run")
    harness = exp.baseline_commit if arm == "baseline" else exp.candidate_commit
    started = ctx.now()
    cancel_event = getattr(ctx, "cancel_event", None)
    ws = None
    error_code, detail, checks, res = None, None, [], None
    try:
        try:
            ws = replay.materialize(ctx.project_dir, case, harness, exp.fixture_commit, run_id,
                                    editable_globs=harness_globs(cfg), data_dir=ctx.data_dir)
        except (replay.ReplayError, git_ops.GitError) as exc:
            error_code, detail = exc.code, exc.detail
        if ws is not None:
            res = replay.run_arm(adapter, ws, case, arm, exp.model, cfg,
                                 conn_factory=lambda: store.connect(ctx.db_path),
                                 job_id=getattr(ctx, "job_id", None), attempt=attempt, cancel_event=cancel_event)
            if res.error_code in STOP_STATUS:
                return None, res.error_code
            error_code, detail = res.error_code, res.detail
            if error_code is None:
                checks = graders.run_graders(case, ws.path, answer=res.answer, tool_uses=res.tool_uses,
                                             fixture_digests=ws.fixture_digests, cancel_event=cancel_event)
            elif error_code == "timeout":
                # The workspace is still there: file graders can still see an
                # edit made before the hang. Answer and tool evidence is partial.
                on_disk = {"graders": [g for g in case.graders if g.get("type") in TIMEOUT_GRADER_TYPES]}
                checks = graders.run_graders(on_disk, ws.path, answer=None, tool_uses=None,
                                             fixture_digests=ws.fixture_digests, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set() and any(c.get("status") == "error" for c in checks):
            # Grading cut short by a stop request is not a result. A run that
            # finished and was fully graded is stored; run_pairs stops after it.
            return None, "cancelled"
    finally:
        if ws is not None:
            try:
                replay.cleanup(ws)
            except Exception:
                pass

    outcome = graders.outcome_for(checks, error_code)
    env = dict(res.environment) if res is not None else {}
    artifacts = {
        "answer": (res.answer or "")[:ANSWER_CHARS] if res is not None and res.answer is not None else None,
        "tool_uses": list(res.tool_uses) if res is not None else [],
        "permission_denials": list(res.permission_denials or [])[:50] if res is not None else [],
        "call_id": res.call_id if res is not None else None,
        "detail": (detail or "")[:300] or None,
        "fixture_digests": dict(ws.fixture_digests) if ws is not None else {},
        "overlaid_paths": list(ws.overlaid_paths) if ws is not None else [],
    }
    row = {
        "run_id": run_id,
        "candidate_id": exp.candidate_id,
        "case_id": case.id,
        "arm": arm,
        "display_label": display[0],
        "display_order": display[1],
        "trial": trial,
        "attempt": attempt,
        "fixture_digest": ws.fixture_digest if ws is not None else None,
        "harness_commit": ws.arm_commit if ws is not None else harness,
        "model": res.model if res is not None else None,
        "cli_build": env.get("cli_build"),
        "environment_json": canonical_json(env),
        "outcome": outcome,
        "checks_json": canonical_json(checks),
        "usage_json": canonical_json(res.usage) if res is not None and res.usage is not None else None,
        "artifacts_json": canonical_json(artifacts),
        "started_at": iso(started),
        "finished_at": iso(ctx.now()),
        "error_code": error_code,
        "duration_ms": res.duration_ms if res is not None else 0,
    }
    try:
        _insert_run(conn, row)
    except sqlite3.IntegrityError:
        if cancel_event is not None and cancel_event.is_set():
            # The lease was lost and another worker already stored this slot.
            return None, "cancelled"
        raise
    return row, None


def _stop(code: str, detail: str):
    status = STOP_STATUS[code]
    return _result(status, None, code, detail)


def run_pairs(ctx, adapter, conn, exp: Experiment):
    """Run every missing (case, trial, arm). Returns a HandlerResult when the job
    must stop (cancelled, blocked), else None."""
    cancel_event = getattr(ctx, "cancel_event", None)
    rows = phase_rows(conn, exp.candidate_id, exp.first_attempt)
    exp.model = exp.model or _frozen_model(ctx.config, rows)
    slots = _slot_rows(rows)
    for case in exp.cases:
        for trial in range(1, _trials(case) + 1):
            order = arm_order(exp.candidate_id, case.id, trial)
            for position, arm in enumerate(order, start=1):
                existing = slots.get((case.id, arm, trial), [])
                while not _slot_done(existing):
                    if cancel_event is not None and cancel_event.is_set():
                        return _stop("cancelled", "stop requested between runs")
                    attempt = existing[-1]["attempt"] + 1 if existing else exp.first_attempt
                    row, stop = _run_one(ctx, adapter, conn, exp, case, arm, trial, attempt,
                                         ("AB"[position - 1], position))
                    if stop is not None:
                        return _stop(stop, f"run {case.id} {arm} trial {trial} stopped: {stop}")
                    existing = existing + [row]
                    if exp.model is None and row.get("model"):
                        exp.model = row["model"]
    return None


def run_judges(ctx, adapter, conn, exp: Experiment):
    """Judge trial 1 of each case with a judge block. Verdicts are stored on the
    candidate arm's row, so a resumed job does not judge again."""
    from . import judge

    slots = _slot_rows(phase_rows(conn, exp.candidate_id, exp.first_attempt))
    for case in exp.cases:
        if not _get(case, "judge"):
            continue
        base = _effective(slots.get((case.id, "baseline", 1), []))
        cand = _effective(slots.get((case.id, "candidate", 1), []))
        if base is None or cand is None:
            continue
        if cand.get("judge_json"):
            exp.judge_results[case.id] = _loads(cand["judge_json"], {})
            continue
        if base["outcome"] == "infrastructure_error" or cand["outcome"] == "infrastructure_error":
            continue
        result = judge.compare(
            adapter, case, _view(base), _view(cand), _loads(base["checks_json"], []), _loads(cand["checks_json"], []),
            order_seed=f"{exp.candidate_id}:{case.id}:{exp.first_attempt}",
            conn_factory=lambda: store.connect(ctx.db_path), data_dir=ctx.data_dir, cfg=ctx.config,
            job_id=getattr(ctx, "job_id", None), attempt=getattr(ctx, "attempt", 1),
            cancel_event=getattr(ctx, "cancel_event", None), root=getattr(ctx, "plugin_root", None),
        )
        if result.error_code in STOP_STATUS:
            return _stop(result.error_code, f"judge for {case.id} stopped: {result.error_code}")
        verdict = result.as_dict()
        exp.judge_results[case.id] = verdict
        with store.transaction(conn):
            conn.execute("UPDATE evaluation_runs SET judge_json = ? WHERE run_id = ?",
                         (canonical_json(verdict), cand["run_id"]))
    return None


def _view(row: dict) -> dict:
    art = _loads(row.get("artifacts_json"), {})
    return {"answer": art.get("answer"), "tool_uses": art.get("tool_uses") or [],
            "permission_denials": art.get("permission_denials") or []}


def gate_inputs(ctx, conn, candidate: dict, exp: Experiment, *, evaluator_before, evaluator_after,
                policy_start, policy_now) -> dict:
    return {
        "candidate": candidate,
        "cases": exp.cases,
        "runs": [gate_run(r) for r in phase_rows(conn, exp.candidate_id, exp.first_attempt)],
        "judge_results": dict(exp.judge_results),
        "static_validation": _loads(candidate.get("validation_json"), {}),
        "evaluator_before": evaluator_before,
        "evaluator_after": evaluator_after,
        "policy_digest_start": policy_start,
        "policy_digest_now": policy_now,
        "limits": {"experiment_timeout_seconds": ctx.config.get("experiment_timeout_seconds"),
                   "max_attempts": MAX_RUN_ATTEMPTS},
        "consolidation": _consolidation_metric(ctx, candidate, exp),
    }


def _consolidation_metric(ctx, candidate: dict, exp: Experiment) -> dict | None:
    """Loaded skill token estimates (SKILL.md characters / 4) of the involved
    skills at the baseline and candidate commits; None when not a
    consolidation or not computable (the gate then reports it unknown)."""
    if candidate.get("change_kind") != "consolidation":
        return None
    try:
        from . import skills

        dirs = skills.candidate_skill_dirs(candidate)
        if not dirs:
            return None
        return {
            "baseline_tokens": skills.skill_tokens_at(ctx.project_dir, exp.baseline_commit, dirs),
            "candidate_tokens": skills.skill_tokens_at(ctx.project_dir, exp.candidate_commit, dirs),
            "skills": list(dirs),
        }
    except Exception:
        return None


def run_experiment(ctx, adapter, conn, exp: Experiment):
    """Budget check, paired runs and judges. Returns a HandlerResult to stop
    with, or None when every required result is recorded."""
    rows = phase_rows(conn, exp.candidate_id, exp.first_attempt)
    judged = {r["case_id"] for r in rows if r.get("judge_json") and r["arm"] == "candidate" and r["trial"] == 1}
    needed = calls_needed(exp.cases, rows, judged)
    left = experiment_calls_left(conn, ctx.config, ctx.now())
    if needed > left:
        return _result("blocked_budget", None, "budget_exhausted",
                       f"{needed} experiment calls needed, {max(left, 0)} left today")
    stop = run_pairs(ctx, adapter, conn, exp)
    if stop is not None:
        return stop
    return run_judges(ctx, adapter, conn, exp)


# Handler


def evaluate_handler(ctx, job):
    payload = _loads(job["payload_json"], {})
    candidate_id = payload.get("candidate_id") or job["candidate_id"]
    conn = store.connect(ctx.db_path)
    try:
        cand = _candidate(conn, candidate_id)
        if cand is None:
            return _result("succeeded", {"skipped": "candidate_deleted"})
        if cand["status"] not in EVALUABLE_STATES:
            return _result("succeeded", {"skipped": f"candidate_{cand['status']}"})
        with ctx.experiment_lock() as held:
            if not held:
                return _result("retry", None, "experiment_lock_busy", "another project is running an experiment",
                               retry_after_seconds=EXPERIMENT_LOCK_RETRY_SECONDS)
            return _evaluate(ctx, job, conn, cand)
    finally:
        conn.close()


def _candidate(conn, candidate_id) -> dict | None:
    if not candidate_id:
        return None
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def _set(conn, candidate_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with store.transaction(conn):
        conn.execute(f"UPDATE candidates SET {sets} WHERE candidate_id = ?", (*cols.values(), candidate_id))


def _evaluate(ctx, job, conn, cand: dict):
    from . import cases as cases_mod
    from . import claude_cli, gate

    repo = ctx.project_dir
    cid = cand["candidate_id"]
    base = cand["base_commit"]
    if not cand.get("candidate_commit"):
        return _result("failed", None, "candidate_commit_missing", f"candidate {cid} has no commit")

    loaded, errors = cases_mod.load_cases(repo, base)
    by_id = {c.id: c for c in loaded}
    if cand["status"] == "evaluating" and cand.get("evaluator_commit"):
        ids = _loads(cand.get("required_cases_json"), [])
        missing = [i for i in ids if i not in by_id]
        if missing:
            return _finish(ctx, conn, cand, "inconclusive", "invalid_cases",
                           detail=f"preregistered cases missing at base: {', '.join(missing)}")
        required = [by_id[i] for i in ids]
        identity_before = _loads(cand["evaluator_commit"], {})
        policy_start = cand.get("policy_digest")
    else:
        if errors:
            _preregister(ctx, conn, cand, None, None, None)
            return _finish(ctx, conn, cand, "inconclusive", "invalid_cases",
                           detail="; ".join(f"{e.get('path')}: {e.get('code')}" for e in errors)[:1000])
        required = cases_mod.select_required(loaded, cand)
        identity_before = evaluator_identity(ctx, repo, base)
        policy_start = policy_digest(repo)
        _preregister(ctx, conn, cand, [c.id for c in required], identity_before, policy_start)
    cand = _candidate(conn, cid)

    missing = missing_required(cand, required)
    if missing is not None:
        return _finish(ctx, conn, cand, "inconclusive", "no_required_cases", detail=missing)

    exp = Experiment(candidate_id=cid, cases=required, baseline_commit=base,
                     candidate_commit=cand["candidate_commit"], fixture_commit=base)
    adapter = claude_cli.adapter_for(ctx)
    stop = run_experiment(ctx, adapter, conn, exp)
    if stop is not None:
        _set(conn, cid, reason=stop.error_code, updated_at=iso(ctx.now()))
        return stop

    inputs = gate_inputs(ctx, conn, cand, exp, evaluator_before=identity_before,
                         evaluator_after=evaluator_identity(ctx, repo, base),
                         policy_start=policy_start, policy_now=policy_digest(repo))
    decision = gate.decide(inputs)
    status = {"accept": "accepted", "reject": "rejected"}.get(decision["decision"], "inconclusive")
    reason = decision["reason_codes"][0] if decision["reason_codes"] else None
    return _finish(ctx, conn, cand, status, reason, gate_result=decision)


def _preregister(ctx, conn, cand: dict, ids: list[str] | None, identity, policy) -> None:
    """Record the required cases (None keeps the stored list), evaluator identity
    and policy digest, and move the candidate to evaluating, before any run."""
    stored = cand.get("required_cases_json") or "[]"
    with store.transaction(conn):
        conn.execute(
            "UPDATE candidates SET required_cases_json = ?, evaluator_commit = ?, policy_digest = ?, "
            "status = 'evaluating', reason = NULL, updated_at = ? WHERE candidate_id = ? AND status IN (?, ?)",
            (stored if ids is None else canonical_json(ids),
             canonical_json(identity) if identity is not None else None, policy,
             iso(ctx.now()), cand["candidate_id"], *EVALUABLE_STATES),
        )


def remove_candidate_worktree(ctx, cand: dict) -> bool:
    """Remove the candidate's worktree (the branch is kept). Best effort."""
    from . import git_ops

    path = cand.get("worktree_path")
    if not path:
        return True
    try:
        git_ops.remove_worktree(ctx.project_dir, path, delete_branch=None,
                                worktrees_root=Path(ctx.data_dir) / "worktrees")
        return True
    except (git_ops.GitError, ValueError, OSError):
        return False


def _finish(ctx, conn, cand: dict, status: str, reason: str | None, *, gate_result: dict | None = None,
            detail: str | None = None):
    cid = cand["candidate_id"]
    if gate_result is None:
        gate_result = {"decision": "inconclusive", "reason_codes": [reason] if reason else [],
                       "required_run_ids": [], "checks": {}, "detail": detail}
    removed = status != "accepted" and remove_candidate_worktree(ctx, cand)
    cfg = ctx.config

    def commit(c) -> None:
        now = ctx.now()
        sets = "status = ?, reason = ?, gate_json = ?, updated_at = ?"
        args = [status, reason, canonical_json(gate_result), iso(now)]
        if removed:
            sets += ", worktree_path = NULL"
        cur = c.execute(f"UPDATE candidates SET {sets} WHERE candidate_id = ? AND status = 'evaluating'",
                        (*args, cid))
        if cur.rowcount != 1:
            raise RuntimeError(f"candidate {cid} is no longer evaluating")
        if status == "accepted" and cfg.get("automatic_apply") and not _paused(c):
            queue.enqueue(c, "apply", f"apply:{cid}", {"candidate_id": cid}, now, candidate_id=cid)

    result = {"candidate_id": cid, "status": status, "reason": reason,
              "decision": gate_result.get("decision"), "reason_codes": gate_result.get("reason_codes")}
    return _result("succeeded", result, commit=commit)


def _paused(conn) -> bool:
    value = store.get_meta(conn, "paused")
    return value is not None and value.strip().lower() in ("1", "true", "yes")


def _register() -> None:
    from . import worker

    worker.register_handler("evaluate", evaluate_handler)


_register()
