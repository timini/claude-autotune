"""Command line entry: `python3 -m autotune <command>`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import __version__, config, diskcap, ingress, locks, paths, queue, retention, store
from .util import canonical_json, iso, utc_now

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# Fields written by `enable`. Worker tuning keys keep their defaults implicitly.
_ENABLE_KEYS = (
    "enabled",
    "automatic_apply",
    "trigger",
    "provider",
    "replay",
    "editable_paths",
    "executable_paths",
    "review_calls_per_day",
    "experiment_calls_per_day",
    "experiments_per_day",
    "experiment_timeout_seconds",
    "consolidate_after_tool_calls",
    "consolidate_after_skill_changes",
    "raw_retention_days",
    "result_retention_days",
)


_MODEL_ROLE_GROUPS = (
    ("review", ("review",), "review_calls_per_day"),
    ("experiment", ("author", "judge", "curator", "replay"), "experiment_calls_per_day"),
)


class CliError(Exception):
    pass


class Project:
    def __init__(self, project_dir: str | None) -> None:
        start = project_dir or os.getcwd()
        self.facts = ingress.git_facts(start)
        if not self.facts["toplevel"] or not self.facts["git_common_dir"]:
            raise CliError(f"not inside a Git worktree: {start}")
        self.toplevel = self.facts["toplevel"]
        self.common_dir = self.facts["git_common_dir"]

    @property
    def data_dir_path(self) -> Path:
        """Data dir location without creating it."""
        return paths.base_dir() / paths.project_id(self.common_dir)

    def data_dir(self) -> Path:
        return paths.project_data_dir(self.common_dir)

    def config(self) -> dict | None:
        return config.load_config(self.toplevel)

    def require_enrolled(self) -> dict:
        cfg = self.config()
        if cfg is None:
            raise CliError(f"project is not enrolled (no {config.CONFIG_FILENAME}); run `autotune enable`")
        return cfg

    def connect(self) -> sqlite3.Connection:
        return store.connect(paths.db_path(self.data_dir()))

    def existing_db(self) -> sqlite3.Connection | None:
        db = paths.db_path(self.data_dir_path)
        if not db.exists():
            return None
        return store.connect(db)

    def worker_running(self) -> bool:
        return locks.is_locked(paths.worker_lock_path(self.data_dir_path))


# status


def _status_data(project: Project) -> dict:
    data: dict = {
        "project": {
            "toplevel": project.toplevel,
            "git_common_dir": project.common_dir,
            "project_id": paths.project_id(project.common_dir),
            "data_dir": str(project.data_dir_path),
        },
        "enrollment": None,
        "enrollment_error": None,
        "paused": False,
        "worker_running": project.worker_running(),
        "queue": None,
        "recent_episodes": [],
        "blocked_jobs": [],
        "capture_errors_24h": {"count": 0, "recent": []},
        "model_calls_today": None,
        "latest_review": None,
        "candidates": {},
        "disk": None,
        "fallback_capture_errors": 0,
        "experiments": [],
        "waiting_to_apply": [],
        "applied_unobserved": [],
        "regressions": [],
        "apply_paused_reason": None,
        "git_reconciliation_required": None,
    }
    cfg = None
    try:
        cfg = project.config()
        data["enrollment"] = None if cfg is None else ("enabled" if cfg["enabled"] else "disabled")
    except config.ConfigError as exc:
        data["enrollment"] = "invalid"
        data["enrollment_error"] = str(exc)[:300]
    if project.data_dir_path.exists():
        usage = diskcap.usage(project.data_dir_path)
        cap = diskcap.cap_bytes(cfg or config.DEFAULTS)
        data["disk"] = {**usage, "cap_bytes": cap, "over_cap": usage["capped_total"] > cap}
        data["fallback_capture_errors"] = diskcap.fallback_count(project.data_dir_path)

    conn = project.existing_db()
    if conn is None:
        return data
    try:
        data["paused"] = (store.get_meta(conn, "paused") or "0") == "1"
        data["queue"] = store.queue_counts(conn)
        for r in conn.execute(
            "SELECT episode_id, completed_at, end_reason, review_status FROM episodes "
            "ORDER BY completed_at DESC, captured_at DESC LIMIT 5"
        ):
            data["recent_episodes"].append(dict(r))
        for r in conn.execute(
            "SELECT id, job_type, state, last_error, updated_at FROM jobs "
            "WHERE state LIKE 'blocked_%' ORDER BY updated_at DESC, id DESC LIMIT 10"
        ):
            data["blocked_jobs"].append(
                {"job_id": r["id"], "job_type": r["job_type"], "state": r["state"],
                 "reason": r["last_error"], "updated_at": r["updated_at"]}
            )
        since = iso(utc_now() - timedelta(hours=24))
        row = conn.execute("SELECT COUNT(*) AS n FROM capture_errors WHERE occurred_at >= ?", (since,)).fetchone()
        data["capture_errors_24h"]["count"] = row["n"]
        for r in conn.execute(
            "SELECT code, occurred_at FROM capture_errors WHERE occurred_at >= ? "
            "ORDER BY occurred_at DESC LIMIT 5",
            (since,),
        ):
            data["capture_errors_24h"]["recent"].append(dict(r))
        data["model_calls_today"] = _model_calls_today(conn, utc_now(), cfg)
        data["latest_review"] = _latest_review(conn)
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status ORDER BY status"):
            data["candidates"][r["status"]] = r["n"]
        data.update(_application_status(conn, project.toplevel))
    finally:
        conn.close()
    return data


def _model_calls_today(conn, now: datetime, cfg: dict | None) -> dict:
    start = datetime.combine(now.astimezone(timezone.utc).date(), dtime(0), tzinfo=timezone.utc)
    roles: dict[str, int] = {}
    for r in conn.execute(
        "SELECT role, COUNT(*) AS n FROM model_calls WHERE started_at >= ? AND started_at < ? GROUP BY role",
        (iso(start), iso(start + timedelta(days=1))),
    ):
        roles[r["role"]] = r["n"]
    groups = {}
    for name, members, limit_key in _MODEL_ROLE_GROUPS:
        groups[name] = {
            "used": sum(roles.get(m, 0) for m in members),
            "limit": (cfg or config.DEFAULTS)[limit_key],
            "roles": {m: roles.get(m, 0) for m in members},
        }
    return groups


def _latest_review(conn) -> dict | None:
    r = conn.execute(
        "SELECT review_id, episode_id, status, task_stage, recommendation, failure_signature, created_at "
        "FROM reviews ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


# experiments, application and activation


EXPERIMENT_STATES = ("validating", "evaluating")
WAITING_STATES = ("accepted", "waiting_to_apply")
REVERTIBLE_STATES = ("applied", "observed_active", "regression_suspected")
GATE_CHECKS = ("integrity", "static", "critical", "known_successes", "target_effect", "judgment", "cost",
               "completeness")
APPLIED_UNOBSERVED_NOTE = (
    "applied; takes effect in new sessions or after the files reload; exposure not observed yet"
)


def _loads(text, default):
    if text is None:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _json_or_none(text):
    try:
        return json.loads(text) if text is not None else None
    except (TypeError, ValueError):
        return None


def _required_case_ids(entries: list) -> list[str]:
    out = []
    for e in entries:
        cid = e.get("id") or e.get("case_id") if isinstance(e, dict) else e
        if isinstance(cid, str) and cid not in out:
            out.append(cid)
    return out


def _case_trials(repo, base, entries: list) -> dict[str, int] | None:
    """Trials per arm for each preregistered case: from the entry itself
    when it carries them, else from the cases committed at the base."""
    trials: dict[str, int] = {}
    missing = []
    for e in entries:
        if isinstance(e, dict):
            n = e.get("trials_per_arm", e.get("trials"))
            cid = e.get("id") or e.get("case_id")
            if isinstance(cid, str) and type(n) is int:
                trials[cid] = n
                continue
        missing.extend(_required_case_ids([e]))
    if not missing:
        return trials
    try:
        from . import cases

        loaded, _ = cases.load_cases(repo, base)
    except Exception:
        return None
    by_id = {}
    for c in loaded:
        cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
        n = c.get("trials_per_arm") if isinstance(c, dict) else getattr(c, "trials_per_arm", None)
        by_id[cid] = n
    for cid in missing:
        if type(by_id.get(cid)) is not int:
            return None
        trials[cid] = by_id[cid]
    return trials


def _runs_done(conn, candidate_id: str) -> int:
    """Finished (case, arm, trial) slots; a retried attempt counts once."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT case_id, arm, trial FROM evaluation_runs "
        "WHERE candidate_id = ? AND outcome IS NOT NULL)",
        (candidate_id,),
    ).fetchone()
    return row["n"]


def _latest_job(conn, candidate_id: str, job_types) -> dict | None:
    marks = ", ".join("?" for _ in job_types)
    r = conn.execute(
        f"SELECT id, job_type, state, last_error, last_error_detail, next_eligible_at, payload_json, updated_at "
        f"FROM jobs WHERE candidate_id = ? AND job_type IN ({marks}) ORDER BY id DESC LIMIT 1",
        (candidate_id, *job_types),
    ).fetchone()
    if r is None:
        return None
    return {
        "job_id": r["id"], "job_type": r["job_type"], "state": r["state"], "reason": r["last_error"],
        "detail": (r["last_error_detail"] or "")[:300] or None, "next_eligible_at": r["next_eligible_at"],
        "payload": _loads(r["payload_json"], {}), "updated_at": r["updated_at"],
    }


def _application_status(conn, repo) -> dict:
    out: dict = {"experiments": [], "waiting_to_apply": [], "applied_unobserved": [], "regressions": []}
    marks = ", ".join("?" for _ in EXPERIMENT_STATES)
    for r in conn.execute(
        f"SELECT candidate_id, status, reason, base_commit, required_cases_json FROM candidates "
        f"WHERE status IN ({marks}) ORDER BY created_at",
        EXPERIMENT_STATES,
    ):
        entries = _loads(r["required_cases_json"], [])
        trials = _case_trials(repo, r["base_commit"], entries) if entries else {}
        required = None if trials is None else sum(2 * n for n in trials.values())
        out["experiments"].append({
            "candidate_id": r["candidate_id"], "status": r["status"], "reason": r["reason"],
            "cases": len(_required_case_ids(entries)), "runs_done": _runs_done(conn, r["candidate_id"]),
            "runs_required": required,
        })
    marks = ", ".join("?" for _ in WAITING_STATES)
    for r in conn.execute(
        f"SELECT candidate_id, status, reason FROM candidates WHERE status IN ({marks}) ORDER BY created_at",
        WAITING_STATES,
    ):
        out["waiting_to_apply"].append({
            "candidate_id": r["candidate_id"], "status": r["status"], "reason": r["reason"],
            "apply_job": _latest_job(conn, r["candidate_id"], ("apply",)),
        })
    for r in conn.execute(
        "SELECT candidate_id, applied_commit, applied_at, exposure FROM candidates "
        "WHERE status = 'applied' ORDER BY applied_at, created_at"
    ):
        out["applied_unobserved"].append({
            "candidate_id": r["candidate_id"], "applied_commit": r["applied_commit"],
            "applied_at": r["applied_at"], "exposure": r["exposure"] or "unknown",
        })
    for r in conn.execute(
        "SELECT candidate_id, reason FROM candidates WHERE status = 'regression_suspected' ORDER BY updated_at"
    ):
        out["regressions"].append({
            "candidate_id": r["candidate_id"], "reason": r["reason"],
            "job": _latest_job(conn, r["candidate_id"], ("reproduce_regression",)),
        })
    out["apply_paused_reason"] = store.get_meta(conn, "apply_paused_reason") or None
    out["git_reconciliation_required"] = store.get_meta(conn, "git_reconciliation_required") or None
    return out


def _job_line(job: dict | None) -> str:
    if job is None:
        return "no job queued"
    text = f"job {job['job_id']} {job['state']}"
    if job["reason"]:
        text += f": {job['reason']}"
        if job["detail"]:
            text += f" ({job['detail']})"
    if job["state"] == "retry_wait":
        text += f", next check {job['next_eligible_at']}"
    return text


def _format_application(d: dict) -> list[str]:
    lines = []
    if d.get("experiments"):
        lines.append("Experiments in progress:")
        for e in d["experiments"]:
            required = "?" if e["runs_required"] is None else str(e["runs_required"])
            lines.append(f"  {e['candidate_id']}  {e['status']}  runs {e['runs_done']}/{required} "
                         f"over {e['cases']} case(s)")
    if d.get("waiting_to_apply"):
        lines.append("Accepted, waiting to apply:")
        for w in d["waiting_to_apply"]:
            reason = f" ({w['reason']})" if w["reason"] else ""
            lines.append(f"  {w['candidate_id']}  {w['status']}{reason}; {_job_line(w['apply_job'])}")
    if d.get("applied_unobserved"):
        lines.append("Applied, not yet observed active:")
        for a in d["applied_unobserved"]:
            commit = (a["applied_commit"] or "")[:12]
            lines.append(f"  {a['candidate_id']}  {commit}  {APPLIED_UNOBSERVED_NOTE}")
    if d.get("regressions"):
        lines.append("Suspected regressions:")
        for r in d["regressions"]:
            reason = f" ({r['reason']})" if r["reason"] else ""
            blocker = ""
            if r["reason"] == "revert_conflict":
                blocker = "; the revert conflicts with later commits, resolve by hand or run `autotune revert` again"
            lines.append(f"  {r['candidate_id']}{reason}; {_job_line(r['job'])}{blocker}")
    if d.get("apply_paused_reason"):
        lines.append(f"Application paused: {d['apply_paused_reason']}")
    if d.get("git_reconciliation_required"):
        lines.append(f"Git reconciliation required: {d['git_reconciliation_required']} "
                     "(application is suspended; inspect the checkout)")
    return lines


def _format_status(d: dict) -> str:
    lines = [f"Autotune status for {d['project']['toplevel']}"]
    enrollment = d["enrollment"] or "not enrolled"
    if d["enrollment_error"]:
        enrollment += f" ({d['enrollment_error']})"
    lines.append(f"Enrollment: {enrollment}")
    lines.append(f"Paused: {'yes' if d['paused'] else 'no'}")
    lines.append(f"Worker: {'running' if d['worker_running'] else 'not running'}")
    q = d["queue"]
    if q is None:
        lines.append("Queue: no data recorded yet")
    else:
        lines.append(
            f"Queue: {q['queued']} queued, {q['leased']} running, {q['retry_wait']} waiting to retry, "
            f"{q['blocked']} blocked, {q['failed']} failed, {q['succeeded']} done"
        )
    if d["recent_episodes"]:
        lines.append("Recent episodes:")
        for e in d["recent_episodes"]:
            lines.append(f"  {e['episode_id']}  {e['completed_at']}  {e['end_reason']}  review {e['review_status']}")
    else:
        lines.append("Recent episodes: none")
    if d["blocked_jobs"]:
        lines.append("Blocked jobs:")
        for j in d["blocked_jobs"]:
            lines.append(f"  job {j['job_id']} ({j['job_type']}) {j['state']}: {j['reason'] or 'no reason recorded'}")
    ce = d["capture_errors_24h"]
    lines.append(f"Capture errors in the last 24 hours: {ce['count']}")
    for e in ce["recent"]:
        lines.append(f"  {e['occurred_at']}  {e['code']}")
    if d["fallback_capture_errors"]:
        lines.append(f"Capture errors written outside the database (disk cap or disk full): "
                     f"{d['fallback_capture_errors']}")
    mc = d["model_calls_today"]
    if mc is not None:
        parts = []
        for name, g in mc.items():
            roles = ", ".join(f"{role} {n}" for role, n in g["roles"].items())
            parts.append(f"{name} {g['used']}/{g['limit']} ({roles})")
        lines.append("Model calls today (UTC): " + "; ".join(parts))
    lr = d["latest_review"]
    if lr is not None:
        sig = f", signature {lr['failure_signature']}" if lr["failure_signature"] else ""
        lines.append(f"Latest review: {lr['review_id']} for {lr['episode_id']} at {lr['created_at']}: "
                     f"{lr['status']}, {lr['task_stage']}, {lr['recommendation']}{sig}")
    elif d["queue"] is not None:
        lines.append("Latest review: none")
    if d["candidates"]:
        lines.append("Candidates: " + ", ".join(f"{n} {status}" for status, n in d["candidates"].items()))
    lines.extend(_format_application(d))
    disk = d["disk"]
    if disk is not None:
        over = " (over the cap)" if disk["over_cap"] else ""
        lines.append(
            f"Disk: {_mib(disk['capped_total'])} of {_mib(disk['cap_bytes'])} cap{over} "
            f"(database {_mib(disk['db'] + disk['db_wal'] + disk['db_shm'])}, logs {_mib(disk['logs'])}); "
            f"worktrees {_mib(disk['worktrees'])}"
        )
    return "\n".join(lines)


def _mib(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MiB"


def cmd_status(project: Project, args) -> int:
    data = _status_data(project)
    if args.json:
        print(json.dumps(data, sort_keys=True))
    else:
        print(_format_status(data))
    return EXIT_OK


# enrollment and controls


def cmd_enable(project: Project, args) -> int:
    path = Path(project.toplevel) / config.CONFIG_FILENAME
    if path.exists():
        try:
            cfg = config.load_config(project.toplevel)
        except config.ConfigError as exc:
            print(f"{path} exists but is invalid: {exc}")
            return EXIT_FAIL
        state = "enabled" if cfg["enabled"] else "present but disabled (set \"enabled\": true)"
        print(f"{path} already exists and is valid; left unchanged. Enrollment: {state}")
        return EXIT_OK
    body = {k: config.DEFAULTS[k] for k in _ENABLE_KEYS}
    body["enabled"] = True
    body["automatic_apply"] = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        print(f"{path} appeared while enabling; left unchanged")
        return EXIT_FAIL
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
        fh.write("\n")
    try:
        config.load_config(project.toplevel)
    except config.ConfigError as exc:
        print(f"wrote {path} but it failed validation: {exc}")
        return EXIT_FAIL
    print(f"Wrote {path} (automatic_apply is false). Commit it to enroll this repository.")
    return EXIT_OK


def cmd_pause(project: Project, args) -> int:
    project.require_enrolled()
    conn = project.connect()
    try:
        store.set_meta(conn, "paused", "1")
    finally:
        conn.close()
    print("Paused. Capture continues; no new learning jobs will be dispatched.")
    return EXIT_OK


def cmd_resume(project: Project, args) -> int:
    project.require_enrolled()
    conn = project.connect()
    try:
        store.set_meta(conn, "paused", "0")
    finally:
        conn.close()
    worker = ingress.spawn_worker(project.toplevel, project.data_dir(), os.environ)
    print(f"Resumed. Worker: {worker}")
    return EXIT_OK


def cmd_retry(project: Project, args) -> int:
    project.require_enrolled()
    conn = project.connect()
    try:
        n = queue.unblock(conn, utc_now())
    finally:
        conn.close()
    worker = ingress.spawn_worker(project.toplevel, project.data_dir(), os.environ)
    print(f"Requeued {n} blocked job(s). Worker: {worker}")
    return EXIT_OK


def cmd_cancel(project: Project, args) -> int:
    project.require_enrolled()
    conn = project.connect()
    try:
        ok = queue.cancel(conn, args.job_id, utc_now())
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
    finally:
        conn.close()
    if ok and row is not None and row["state"] == "leased":
        print(f"Stop requested for running job {args.job_id}; its worker stops it within a few seconds")
        return EXIT_OK
    if ok:
        print(f"Cancelled job {args.job_id}")
        return EXIT_OK
    if row is None:
        print(f"No job {args.job_id}")
    else:
        print(f"Job {args.job_id} is {row['state']} and cannot be cancelled")
    return EXIT_FAIL


# reviews and candidates


def cmd_review(project: Project, args) -> int:
    conn = project.existing_db()
    if conn is None:
        print("No reviews: no data recorded for this project")
        return EXIT_FAIL
    try:
        try:
            from . import review
        except Exception as exc:
            print(f"autotune: the review module is unavailable ({type(exc).__name__})", file=sys.stderr)
            return EXIT_FAIL
        text = review.format_review(conn, args.ref)
    finally:
        conn.close()
    if text is None:
        print(f"No review found for {args.ref}" if args.ref else "No reviews yet")
        return EXIT_FAIL
    print(text)
    return EXIT_OK


def _candidate_list(conn) -> list[str]:
    lines = []
    for r in conn.execute(
        "SELECT candidate_id, status, reason, hypothesis, created_at FROM candidates "
        "ORDER BY created_at DESC, rowid DESC LIMIT 20"
    ):
        hypothesis = " ".join((r["hypothesis"] or "").split())[:80]
        reason = f" ({r['reason']})" if r["reason"] else ""
        lines.append(f"{r['candidate_id']}  {r['status']}{reason}  {hypothesis}")
    return lines


def cmd_candidate(project: Project, args) -> int:
    conn = project.existing_db()
    if conn is None:
        print("No candidates: no data recorded for this project")
        return EXIT_FAIL if args.candidate_id else EXIT_OK
    try:
        if not args.candidate_id:
            lines = _candidate_list(conn)
            print("\n".join(["Recent candidates:", *lines]) if lines else "No candidates yet")
            return EXIT_OK
        try:
            from . import propose
        except Exception as exc:
            print(f"autotune: the propose module is unavailable ({type(exc).__name__})", file=sys.stderr)
            return EXIT_FAIL
        text = propose.format_candidate(conn, project.toplevel, args.candidate_id)
        if text is not None:
            text = _insert_before_diff(text, _evaluation_detail(conn, args.candidate_id))
    finally:
        conn.close()
    if text is None:
        print(f"No candidate {args.candidate_id}")
        return EXIT_FAIL
    print(text)
    return EXIT_OK


def _insert_before_diff(text: str, extra: list[str]) -> str:
    """Put the evaluation and application detail ahead of the (long) diff."""
    if not extra:
        return text
    lines = text.rstrip("\n").split("\n")
    at = next((i for i, ln in enumerate(lines) if ln.startswith("diff:")), len(lines))
    return "\n".join(lines[:at] + extra + lines[at:]) + "\n"


def _grader_results(checks) -> list[dict]:
    """Grader results from a run's checks_json, which holds a list of
    {id, type, status, detail}, or an object wrapping one."""
    if isinstance(checks, dict):
        for key in ("graders", "results", "checks"):
            if isinstance(checks.get(key), list):
                checks = checks[key]
                break
        else:
            checks = [{"id": k, **v} for k, v in checks.items() if isinstance(v, dict)]
    return [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []


def _evaluation_detail(conn, candidate_id: str) -> list[str]:
    c = conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if c is None:
        return []
    c = dict(c)
    lines = []
    entries = _loads(c.get("required_cases_json"), [])
    lines.append(f"task family: {c.get('task_family') or 'unknown'}")
    lines.append(f"target branch: {c.get('target_branch') or 'unknown'}")
    lines.append(f"preregistered cases: {', '.join(_required_case_ids(entries)) or 'none'}")
    if c.get("evaluator_commit"):
        lines.append(f"evaluator identity: {c['evaluator_commit']}")
    if c.get("policy_digest"):
        lines.append(f"policy digest: {c['policy_digest']}")

    runs = conn.execute(
        "SELECT run_id, case_id, arm, trial, attempt, outcome, error_code, model, checks_json, judge_json, "
        "display_order, duration_ms FROM evaluation_runs WHERE candidate_id = ? "
        "ORDER BY case_id, trial, arm, attempt",
        (candidate_id,),
    ).fetchall()
    if runs:
        lines.append(f"runs ({len(runs)}):")
        for r in runs:
            failed = [g.get("id") or g.get("type") or "?" for g in _grader_results(_json_or_none(r["checks_json"]))
                      if g.get("status") in ("fail", "error")]
            text = (f"  {r['case_id']}  {r['arm']}  trial {r['trial']}  attempt {r['attempt']}  "
                    f"{r['outcome'] or 'running'}")
            if r["error_code"]:
                text += f" ({r['error_code']})"
            if failed:
                text += f"  failed graders: {', '.join(str(f) for f in failed)}"
            if r["model"]:
                text += f"  model {r['model']}"
            lines.append(text)
            judge = _loads(r["judge_json"], {})
            if judge:
                lines.append(f"    judge: {_judge_text(judge)}")
    else:
        lines.append("runs: none")

    gate = _loads(c.get("gate_json"), {})
    if gate:
        codes = ", ".join(str(x) for x in gate.get("reason_codes") or []) or "none"
        lines.append(f"gate decision: {gate.get('decision', 'unknown')} (reason codes: {codes})")
        checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
        if checks:
            order = [k for k in GATE_CHECKS if k in checks] + sorted(k for k in checks if k not in GATE_CHECKS)
            lines.append("gate checks: " + ", ".join(f"{k} {checks[k]}" for k in order))
    else:
        lines.append("gate decision: not decided")

    status = c["status"]
    if c.get("applied_commit"):
        lines.append(f"application: applied {c['applied_commit']} at {c.get('applied_at') or 'unknown time'}")
    elif status in WAITING_STATES:
        lines.append(f"application: waiting ({c.get('reason') or 'not attempted yet'})")
    else:
        lines.append("application: not applied")
    if status == "applied":
        lines.append(f"exposure: {c.get('exposure') or 'unknown'} ({APPLIED_UNOBSERVED_NOTE})")
    else:
        lines.append(f"exposure: {c.get('exposure') or 'unknown'}")
    if c.get("reverted_commit"):
        lines.append(f"revert: reverted by {c['reverted_commit']} at {c.get('reverted_at') or 'unknown time'}")
    elif status == "regression_suspected":
        lines.append(f"revert: pending ({c.get('reason') or 'regression suspected'})")
    if c.get("branch_deleted_at"):
        lines.append(f"branch deleted: {c['branch_deleted_at']}")
    apply_job = _latest_job(conn, candidate_id, ("apply",))
    if apply_job is not None:
        lines.append(f"latest apply job: {_job_line(apply_job)}")
    repro = _latest_job(conn, candidate_id, ("reproduce_regression",))
    if repro is not None:
        mode = repro["payload"].get("mode") if isinstance(repro["payload"], dict) else None
        label = "latest revert job" if mode == "revert_now" else "latest reproduction job"
        lines.append(f"{label}: {_job_line(repro)}")
    return lines


def _judge_text(j: dict) -> str:
    """A judge.JudgeResult dict: the combined verdict, then each order-swapped
    call's mapped preference and first reason."""
    verdict = str(j.get("verdict") or "unknown")
    if j.get("error_code"):
        verdict += f" ({j['error_code']})"
    parts = []
    for call in j.get("calls") or []:
        if not isinstance(call, dict):
            continue
        text = f"order {call.get('order') or '?'} {call.get('mapped') or call.get('error_code') or 'no answer'}"
        reasons = call.get("reasons")
        if isinstance(reasons, list) and reasons:
            text += f": {str(reasons[0])[:200]}"
        parts.append(text)
    return f"{verdict}; " + "; ".join(parts) if parts else verdict


# apply and revert

_REARM_STATES = ("succeeded", "failed", "cancelled")


def _enqueue_or_rearm(conn, job_type: str, dedup_key: str, payload: dict, candidate_id: str) -> tuple[int, str]:
    """Queue the job, or reuse the one with this dedup key.

    Returns (job_id, action): "queued" (new), "pending" (already waiting or
    running; a retry wait is made eligible now), or "requeued" (a finished job
    is queued again with fresh attempts).
    """
    now = iso(utc_now())
    with store.transaction(conn):
        row = conn.execute("SELECT id, state FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
        if row is None:
            job_id, _ = queue.enqueue(conn, job_type, dedup_key, payload, utc_now(), candidate_id=candidate_id)
            return job_id, "queued"
        if row["state"] in _REARM_STATES:
            conn.execute(
                "UPDATE jobs SET state = 'queued', attempts = 0, payload_json = ?, next_eligible_at = ?, "
                "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL, last_error = NULL, "
                "last_error_detail = NULL, result_json = NULL, cancel_requested_at = NULL, updated_at = ? "
                "WHERE id = ?",
                (canonical_json(payload), now, now, row["id"]),
            )
            return row["id"], "requeued"
        if row["state"] == "retry_wait":
            conn.execute("UPDATE jobs SET state = 'queued', next_eligible_at = ?, updated_at = ? WHERE id = ?",
                         (now, now, row["id"]))
        return row["id"], "pending"


def _candidate_row(conn, candidate_id: str):
    return conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()


def _paused_notes(conn) -> list[str]:
    notes = []
    if (store.get_meta(conn, "paused") or "0") == "1":
        notes.append("Autotune is paused; the job runs after `autotune resume`.")
    reason = store.get_meta(conn, "apply_paused_reason")
    if reason:
        notes.append(f"Application is paused ({reason}); the job waits until that clears.")
    if store.get_meta(conn, "git_reconciliation_required"):
        notes.append("Git reconciliation is required; application stays suspended until the checkout is inspected.")
    return notes


_ACTION_TEXT = {"queued": "Queued", "pending": "Already pending:", "requeued": "Requeued"}


def cmd_apply(project: Project, args) -> int:
    project.require_enrolled()
    conn = project.existing_db()
    if conn is None:
        print("No candidates: no data recorded for this project")
        return EXIT_FAIL
    try:
        c = _candidate_row(conn, args.candidate_id)
        if c is None:
            print(f"No candidate {args.candidate_id}")
            return EXIT_FAIL
        if c["status"] not in WAITING_STATES:
            reason = f" ({c['reason']})" if c["reason"] else ""
            print(f"Candidate {c['candidate_id']} is {c['status']}{reason}; only an accepted candidate can be applied")
            return EXIT_FAIL
        payload = {"candidate_id": c["candidate_id"], "requested_by": "user"}
        job_id, action = _enqueue_or_rearm(conn, "apply", f"apply:{c['candidate_id']}", payload, c["candidate_id"])
        notes = _paused_notes(conn)
    finally:
        conn.close()
    branch = c["target_branch"] or "the branch the base came from"
    print(f"{_ACTION_TEXT[action]} apply job {job_id} for {c['candidate_id']}.")
    print(f"The worker rechecks that {branch} is checked out at base {c['base_commit'][:12]}, that the policy "
          "and evaluator are unchanged, that your checkout has no changes at the candidate's paths, and that "
          f"no session is active, then fast-forwards to {(c['candidate_commit'] or '')[:12]}. "
          "If a check fails it waits and `autotune status` shows why; your files are never overwritten.")
    for note in notes:
        print(note)
    worker = ingress.spawn_worker(project.toplevel, project.data_dir(), os.environ)
    print(f"Worker: {worker}")
    return EXIT_OK


def cmd_revert(project: Project, args) -> int:
    project.require_enrolled()
    conn = project.existing_db()
    if conn is None:
        print("No candidates: no data recorded for this project")
        return EXIT_FAIL
    try:
        c = _candidate_row(conn, args.candidate_id)
        if c is None:
            print(f"No candidate {args.candidate_id}")
            return EXIT_FAIL
        if c["status"] not in REVERTIBLE_STATES or not c["applied_commit"]:
            reason = f" ({c['reason']})" if c["reason"] else ""
            print(f"Candidate {c['candidate_id']} is {c['status']}{reason}; only an applied candidate can be reverted")
            return EXIT_FAIL
        payload = {"candidate_id": c["candidate_id"], "mode": "revert_now", "requested_by": "user"}
        job_id, action = _enqueue_or_rearm(conn, "reproduce_regression", f"revert:{c['candidate_id']}", payload,
                                           c["candidate_id"])
        notes = _paused_notes(conn)
    finally:
        conn.close()
    cid = c["candidate_id"]
    print(f"{_ACTION_TEXT[action]} revert job {job_id} for {cid}.")
    print(f"The worker reverts {c['applied_commit'][:12]} on branch autotune/revert-{cid} in a separate "
          "worktree, checks the revert only touches the candidate's own paths, then fast-forwards your branch "
          "with the same checks as apply. A conflict leaves your checkout unchanged and `autotune status` "
          "shows the blocker.")
    for note in notes:
        print(note)
    worker = ingress.spawn_worker(project.toplevel, project.data_dir(), os.environ)
    print(f"Worker: {worker}")
    return EXIT_OK


def cmd_purge(project: Project, args) -> int:
    if not args.yes:
        what = "evidence content" if args.scope == "evidence" else "all episodes, evidence, reviews and jobs"
        print(f"This permanently deletes {what} for {project.toplevel}. Re-run with --yes to confirm.")
        return EXIT_FAIL
    if args.scope == "all" and project.worker_running():
        print("The worker is running. Run `autotune pause`, wait for it to exit, then purge.")
        return EXIT_FAIL
    conn = project.existing_db()
    if conn is None:
        print("Nothing to purge: no data recorded for this project")
        return EXIT_OK
    try:
        counts = retention.purge(conn, args.scope, None)
    finally:
        conn.close()
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing"
    print(f"Purged ({args.scope}): {summary}")
    return EXIT_OK


# consolidation, calibration, gate mutations


def cmd_consolidate(project: Project, args) -> int:
    project.require_enrolled()
    try:
        from . import review
    except Exception as exc:
        print(f"autotune: the review module is unavailable ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAIL
    conn = project.connect()
    try:
        job_id, action = review.enqueue_sweep(conn, utc_now(), {"mode": "sweep", "requested_by": "user"})
        notes = _paused_notes(conn)
    finally:
        conn.close()
    text = {"queued": "Queued", "requeued": "Requeued", "pending": "Already pending:"}[action]
    print(f"{text} consolidation sweep job {job_id}.")
    print("The worker reads the skills committed at HEAD, picks the most overlapping pair (pinned and critical "
          "skills are never merged away), and proposes a merge only when every trigger, exception and support "
          "file is accounted for. The merge is then evaluated and applied like any other candidate.")
    for note in notes:
        print(note)
    worker = ingress.spawn_worker(project.toplevel, project.data_dir(), os.environ)
    print(f"Worker: {worker}")
    return EXIT_OK


def _hook_module(name: str):
    """Import a tooling module lazily, so a missing or broken one only breaks
    its own command."""
    import importlib

    try:
        module = importlib.import_module(f".{name}", __package__)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    if not callable(getattr(module, "main_args", None)):
        return None, "it has no main_args"
    return module, None


def _run_hook(name: str, args) -> int:
    module, error = _hook_module(name)
    if module is None:
        print(f"autotune: the {name} module is unavailable ({error})", file=sys.stderr)
        return EXIT_FAIL
    code = module.main_args(args)
    return EXIT_OK if code is None else int(code)


def cmd_calibrate(args) -> int:
    if not args.live:
        print("Calibration sends every example to the real reviewer (and pairwise items to the judge), which "
              "uses model calls. Nothing was run. Re-run with --live to run it.")
        return EXIT_USAGE
    return _run_hook("calibrate", args)


def cmd_mutations(args) -> int:
    if args.live and not args.budget_calls:
        print("Live mode runs the real evaluation with the real CLI for every mutation, which uses model calls. "
              "Nothing was run. Pass --budget-calls N (a positive cap on model calls) with --live.")
        return EXIT_USAGE
    return _run_hook("mutations", args)


# export


def cmd_export(project: Project, args) -> int:
    conn = project.existing_db()
    if conn is None:
        print("Nothing to export: no data recorded for this project")
        return EXIT_FAIL
    try:
        from . import export
    except Exception as exc:
        conn.close()
        print(f"autotune: the export module is unavailable ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAIL
    try:
        cfg = project.config() or config.DEFAULTS
        days = cfg.get("raw_retention_days", config.DEFAULTS["raw_retention_days"])
        fixture, report = export.build_export(conn, args.episode_id, project.toplevel, utc_now(),
                                              raw_retention_days=days)
        path = export.write_export(args.out, fixture)
    except export.ExportError as exc:
        print(f"Not exported: {exc.detail}")
        return EXIT_FAIL
    finally:
        conn.close()
    print(export.format_report(path, report))
    return EXIT_OK


# uninstall

WORKER_STOP_SECONDS = 20.0


def _owned_data_dir(project: Project) -> Path:
    """The project data dir, after checking it is the one Autotune names for
    this repository and that it neither holds nor sits inside the checkout."""
    data_dir = Path(os.path.realpath(project.data_dir_path))
    base = Path(os.path.realpath(paths.base_dir()))
    pid = paths.project_id(project.common_dir)
    top = Path(os.path.realpath(project.toplevel))
    if data_dir != base / pid or data_dir.parent != base:
        raise CliError(f"refusing to remove {data_dir}: not the Autotune data dir for this repository")
    for a, b in ((top, data_dir), (data_dir, top)):
        if a == b or b in a.parents:
            raise CliError(f"refusing to remove {data_dir}: it overlaps the checkout {top}")
    return data_dir


def _autotune_worktrees(repo: str, data_dir: Path) -> list[Path]:
    from . import git_ops

    root = data_dir / "worktrees"
    out = []
    for p in git_ops.worktree_paths(repo):
        real = Path(os.path.realpath(p))
        if root in real.parents:
            out.append(real)
    return out


def _autotune_branches(conn) -> list[tuple[str, dict]]:
    """Branches the candidates table says Autotune created: each candidate's
    branch and its revert branch. Nothing is inferred from a name alone."""
    from . import apply

    out = []
    if conn is None:
        return out
    for r in conn.execute("SELECT * FROM candidates ORDER BY created_at, candidate_id"):
        cand = dict(r)
        for branch in (cand.get("branch"), f"autotune/{apply.REVERT_PREFIX}{cand['candidate_id']}"):
            if branch and branch not in (b for b, _ in out):
                out.append((branch, cand))
    return out


def _uninstall_plan(project: Project) -> dict:
    from . import git_ops, retention

    data_dir = _owned_data_dir(project)
    plan = {"data_dir": data_dir if data_dir.exists() else None, "worktrees": [], "branches": [], "kept": []}
    plan["worktrees"] = _autotune_worktrees(project.toplevel, data_dir) if data_dir.exists() else []
    conn = project.existing_db() if data_dir.exists() else None
    try:
        for branch, cand in _autotune_branches(conn):
            if not git_ops.branch_exists(project.toplevel, branch):
                continue
            if retention.made_by_autotune(project.toplevel, branch, cand):
                plan["branches"].append((branch, cand))
            else:
                plan["kept"].append((branch, "it has commits Autotune did not make"))
    finally:
        if conn is not None:
            conn.close()
    return plan


_UNSET = object()


def _swap_paused(project: Project, value) -> object:
    """Set meta paused to `value` (None deletes it) and return the previous
    value, or _UNSET when there is no database."""
    conn = project.existing_db()
    if conn is None:
        return _UNSET
    try:
        previous = store.get_meta(conn, "paused")
        with store.transaction(conn):
            if value is None:
                conn.execute("DELETE FROM meta WHERE key = 'paused'")
            else:
                store.set_meta(conn, "paused", value)
        return previous
    finally:
        conn.close()


def _stop_worker(project: Project, timeout: float) -> bool:
    """Pause learning and stop a running worker without interrupting an
    application. The worker gets SIGTERM only while this process holds the
    apply lock, so no apply or revert is fast-forwarding the user's checkout
    when it stops. On failure the previous paused value is restored."""
    lock = paths.worker_lock_path(project.data_dir_path)
    if not locks.is_locked(lock):
        return True
    previous = _swap_paused(project, "1")
    ok = False
    try:
        ok = _signal_worker_outside_apply(project, lock, timeout)
    finally:
        if not ok and previous is not _UNSET:
            _swap_paused(project, previous)
    return ok


def _signal_worker_outside_apply(project: Project, lock: Path, timeout: float) -> bool:
    import signal
    import time

    from . import apply

    deadline = time.monotonic() + timeout
    apply_lock = locks.FileLock(apply.apply_lock_path(project.data_dir_path), blocking=False)
    try:
        # paused stops new leases; an application already running holds the
        # apply lock until it finishes, so wait for it.
        while not apply_lock.acquire():
            if not locks.is_locked(lock):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        pid = locks.holder_pid(lock)
        if pid is not None and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        while time.monotonic() < deadline:
            if not locks.is_locked(lock):
                return True
            time.sleep(0.1)
        return not locks.is_locked(lock)
    finally:
        apply_lock.release()


def _print_plan(project: Project, plan: dict) -> None:
    print(f"Autotune uninstall for {project.toplevel} removes only what Autotune created:")
    if plan["data_dir"] is not None:
        print(f"  data dir {plan['data_dir']} (evidence, reviews, jobs, logs, candidate records)")
    else:
        print("  no data dir exists")
    for wt in plan["worktrees"]:
        print(f"  worktree {wt}")
    for branch, _ in plan["branches"]:
        print(f"  branch {branch}")
    for branch, why in plan["kept"]:
        print(f"  keeps branch {branch}: {why}")
    print(f"It never touches your checkout, your other branches and worktrees, or {config.CONFIG_FILENAME}.")


def cmd_uninstall(project: Project, args) -> int:
    from . import git_ops, retention

    plan = _uninstall_plan(project)
    if not args.yes:
        _print_plan(project, plan)
        print("Re-run with --yes to confirm.")
        return EXIT_FAIL
    if plan["data_dir"] is not None and not _stop_worker(project, WORKER_STOP_SECONDS):
        print("The worker did not stop (it may be finishing an application or a model call); nothing was "
              "removed and the paused setting is as it was. Run `autotune pause`, wait for the worker to exit, "
              "then run uninstall again.")
        return EXIT_FAIL
    data_dir = plan["data_dir"]
    held = None
    if data_dir is not None:
        # Hold the worker lock so no worker starts while the data dir goes.
        held = locks.FileLock(paths.worker_lock_path(data_dir), blocking=False)
        if not held.acquire():
            print("A worker started again; nothing was removed. The project is left paused; run uninstall "
                  "again, or `autotune resume` to keep it.")
            return EXIT_FAIL
        # The worker may have made a branch or worktree after the first plan.
        fresh = _uninstall_plan(project)
        new = ([f"worktree {w}" for w in fresh["worktrees"] if w not in plan["worktrees"]]
               + [f"branch {b}" for b, _ in fresh["branches"] if b not in [x for x, _ in plan["branches"]]])
        if new:
            print("Also found since the plan was listed: " + ", ".join(new))
        plan = fresh
    removed, kept = [], [f"branch {b}: {why}" for b, why in plan["kept"]]
    try:
        for wt in plan["worktrees"]:
            try:
                git_ops.remove_worktree(project.toplevel, wt, delete_branch=None,
                                        worktrees_root=data_dir / "worktrees")
                removed.append(f"worktree {wt}")
            except (git_ops.GitError, ValueError, OSError) as exc:
                kept.append(f"worktree {wt}: {exc}"[:300])
        for branch, cand in plan["branches"]:
            try:
                outcome = retention.delete_branch(project.toplevel, branch, cand)
            except (git_ops.GitError, ValueError, OSError) as exc:
                outcome = f"error {exc}"[:200]
            if outcome == "deleted":
                removed.append(f"branch {branch}")
            elif outcome != "missing":
                kept.append(f"branch {branch}: {outcome.replace('_', ' ')}")
        if data_dir is not None:
            shutil.rmtree(data_dir)
            removed.append(f"data dir {data_dir}")
    finally:
        if held is not None:
            held.release()
    print("Removed:" if removed else "Nothing to remove.")
    for item in removed:
        print(f"  {item}")
    if kept:
        print("Kept:")
        for item in kept:
            print(f"  {item}")
    if (Path(project.toplevel) / config.CONFIG_FILENAME).exists():
        print(f"{config.CONFIG_FILENAME} is still present, so capture resumes in new sessions while the plugin "
              "is installed. Remove that file, or the plugin, to stop it.")
    return EXIT_OK


# doctor


def _check_mode(path: Path) -> tuple[bool, str]:
    st = path.stat()
    if st.st_uid != os.getuid():
        return False, f"{path} is not owned by the current user"
    if stat.S_IMODE(st.st_mode) & 0o077:
        return False, f"{path} mode is {oct(stat.S_IMODE(st.st_mode))}, expected 0o700"
    return True, f"{path} is private"


def _doctor_checks(project_dir: str | None) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    vi = sys.version_info
    out.append(("python", vi >= (3, 11), f"{vi.major}.{vi.minor}.{vi.micro} (need 3.11+)"))

    git = shutil.which("git")
    if git:
        try:
            r = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=5)
            out.append(("git", r.returncode == 0, r.stdout.strip() or r.stderr.strip()))
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.append(("git", False, type(exc).__name__))
    else:
        out.append(("git", False, "git not found on PATH"))

    sv = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
    out.append(("sqlite", sv >= (3, 35), f"{sqlite3.sqlite_version} (need 3.35+)"))

    claude = shutil.which("claude")
    if claude:
        try:
            r = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL)
            out.append(("claude", r.returncode == 0, (r.stdout.strip() or r.stderr.strip())[:200]))
        except subprocess.TimeoutExpired:
            out.append(("claude", False, "`claude --version` timed out after 5 s"))
        except OSError as exc:
            out.append(("claude", False, type(exc).__name__))
    else:
        out.append(("claude", False, "claude CLI not found on PATH"))

    base = paths.base_dir()
    if base.exists():
        ok, detail = _check_mode(base)
        out.append(("data dir", ok, detail))
    else:
        out.append(("data dir", True, f"{base} not created yet"))

    try:
        project = Project(project_dir)
    except CliError as exc:
        out.append(("enrollment", False, str(exc)))
        return out
    try:
        cfg = project.config()
        if cfg is None:
            out.append(("enrollment", False, f"no {config.CONFIG_FILENAME} in {project.toplevel}"))
        else:
            out.append(("enrollment", bool(cfg["enabled"]),
                        "valid, enabled" if cfg["enabled"] else "valid, but enabled is false"))
    except config.ConfigError as exc:
        out.append(("enrollment", False, f"invalid: {exc}"))
    pdir = project.data_dir_path
    if pdir.exists():
        ok, detail = _check_mode(pdir)
        out.append(("project data dir", ok, detail))
    return out


def cmd_doctor(args) -> int:
    checks = _doctor_checks(args.project_dir)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return EXIT_OK if all(ok for _, ok, _ in checks) else EXIT_FAIL


# parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-dir", default=None, help="repository path (default: Git toplevel of cwd)")

    p = argparse.ArgumentParser(prog="autotune", description="Autotune harness learning")
    p.add_argument("--version", action="version", version=f"autotune {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ingress", parents=[common], help="read one envelope on stdin (used by the mod)")
    w = sub.add_parser("worker", parents=[common], help="run the background worker")
    w.add_argument("--once", action="store_true", help="run at most one job, then exit")
    w.add_argument("--idle-seconds", type=float, default=None)
    s = sub.add_parser("status", parents=[common], help="show queue and recent episodes")
    s.add_argument("--json", action="store_true")
    sub.add_parser("enable", parents=[common], help="write a default .autotune.json if missing")
    sub.add_parser("pause", parents=[common], help="stop dispatching learning jobs")
    sub.add_parser("resume", parents=[common], help="resume dispatching learning jobs")
    sub.add_parser("retry", parents=[common], help="requeue blocked jobs")
    c = sub.add_parser("cancel", parents=[common], help="cancel a job, or ask a running job to stop")
    c.add_argument("job_id", type=int)
    rv = sub.add_parser("review", parents=[common], help="show a review (default: the latest)")
    rv.add_argument("ref", nargs="?", default=None, metavar="EPISODE_OR_REVIEW_ID")
    cd = sub.add_parser("candidate", parents=[common], help="list candidates, or show one")
    cd.add_argument("candidate_id", nargs="?", default=None)
    ap = sub.add_parser("apply", parents=[common], help="queue application of an accepted candidate")
    ap.add_argument("candidate_id")
    rt = sub.add_parser("revert", parents=[common], help="queue a Git revert of an applied candidate")
    rt.add_argument("candidate_id")
    pg = sub.add_parser("purge", parents=[common], help="delete local evidence")
    pg.add_argument("--scope", choices=("evidence", "all"), required=True)
    pg.add_argument("--yes", action="store_true")
    sub.add_parser("doctor", parents=[common], help="check the local environment")
    sub.add_parser("consolidate", parents=[common], help="queue a skill consolidation sweep now")
    cb = sub.add_parser("calibrate", parents=[common], help="measure reviewer agreement on a labelled set")
    cb.add_argument("--set", dest="set_dir", required=True, metavar="DIR")
    cb.add_argument("--out", default=None, metavar="FILE")
    cb.add_argument("--live", action="store_true", help="run the real reviewer (uses model calls)")
    mu = sub.add_parser("mutations", parents=[common], help="run the gate mutation set")
    mu.add_argument("--set", dest="set_dir", default=None, metavar="DIR")
    mu.add_argument("--live", action="store_true", help="run the real evaluation (uses model calls)")
    mu.add_argument("--budget-calls", dest="budget_calls", type=_positive_int, default=None, metavar="N")
    ex = sub.add_parser("export", parents=[common], help="write one episode as a sanitized fixture")
    ex.add_argument("episode_id")
    ex.add_argument("--out", required=True, metavar="FILE")
    un = sub.add_parser("uninstall", parents=[common], help="remove Autotune data, branches and worktrees")
    un.add_argument("--yes", action="store_true")
    return p


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


_PROJECT_COMMANDS = {
    "status": cmd_status,
    "enable": cmd_enable,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "retry": cmd_retry,
    "cancel": cmd_cancel,
    "review": cmd_review,
    "candidate": cmd_candidate,
    "apply": cmd_apply,
    "revert": cmd_revert,
    "purge": cmd_purge,
    "consolidate": cmd_consolidate,
    "export": cmd_export,
    "uninstall": cmd_uninstall,
}


def _test_handlers(env) -> dict | None:
    """Test-only: AUTOTUNE_TEST_HANDLERS names a module whose HANDLERS dict
    replaces the worker's handlers. Honoured only when AUTOTUNE_HOME is set
    (so real data is never touched) and the module is inside the `tests`
    package. Otherwise the variable is ignored."""
    name = env.get("AUTOTUNE_TEST_HANDLERS")
    if not name or not env.get("AUTOTUNE_HOME") or not name.startswith("tests."):
        return None
    import importlib

    handlers = getattr(importlib.import_module(name), "HANDLERS", None)
    return dict(handlers) if isinstance(handlers, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    if args.command == "ingress":
        return ingress.main(sys.stdin.buffer, sys.stdout, os.environ)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "calibrate":
        return cmd_calibrate(args)
    if args.command == "mutations":
        return cmd_mutations(args)
    if args.command == "worker":
        from . import worker

        project_dir = args.project_dir or os.getcwd()
        idle = args.idle_seconds
        if args.once and idle is None:
            idle = 0
        return worker.run_worker(project_dir, idle_seconds=idle, max_jobs=1 if args.once else None,
                                 handlers=_test_handlers(os.environ))

    try:
        project = Project(args.project_dir)
        return _PROJECT_COMMANDS[args.command](project, args)
    except CliError as exc:
        print(f"autotune: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except config.ConfigError as exc:
        print(f"autotune: invalid {config.CONFIG_FILENAME}: {exc}", file=sys.stderr)
        return EXIT_FAIL
