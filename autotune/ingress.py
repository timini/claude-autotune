"""Short-lived ingress: validate one envelope, persist it, wake the worker.

Runs on the foreground path, so it does a bounded amount of work and never
waits on the worker. All Git calls happen before any SQLite transaction.
Writes exactly one JSON line to stdout.

Exit codes: 0 ok (including skips), 2 invalid envelope, 3 persistence failure.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, Mapping, TextIO

from . import capture, config, diskcap, locks, paths, store
from .util import parse_iso, utc_now

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_PERSIST = 3

GIT_TIMEOUT = 0.3
# rev-parse decides whether the turn is captured at all, so it gets more time
# than the optional branch, head and status calls, while staying inside the
# mod's 1 s budget so ingress still answers with its own error.
REV_PARSE_TIMEOUT = 0.7
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Oversized input is read up to this bound so an identity can still be parsed
# for the capture error row. Anything beyond it is not read at all.
_IDENTITY_READ_LIMIT = 4 * capture.MAX_ENVELOPE_BYTES

# Indirection so tests can intercept the worker spawn without affecting git calls.
_popen = subprocess.Popen

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _git(args: list[str], cwd: str, env: dict, timeout: float | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=GIT_TIMEOUT if timeout is None else timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def git_facts(cwd) -> dict:
    """Repository facts for an envelope cwd. Failures become None plus an error code."""
    facts = {
        "toplevel": None,
        "git_common_dir": None,
        "branch": None,
        "head": None,
        "tracked_dirty": None,
        "errors": [],
    }
    cwd = os.fspath(cwd)
    env = _git_env()

    def run(name: str, args: list[str], where: str, timeout: float | None = None):
        try:
            return _git(args, where, env, GIT_TIMEOUT if timeout is None else timeout)
        except subprocess.TimeoutExpired:
            facts["errors"].append(f"{name}:timeout")
        except OSError as exc:
            facts["errors"].append(f"{name}:{type(exc).__name__}")
        return None

    res = run("rev_parse", ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"], cwd,
              REV_PARSE_TIMEOUT)
    if res is None:
        return facts
    code, out = res
    lines = out.splitlines()
    if code != 0 or len(lines) != 2:
        # Git before 2.31 lacks --path-format; a relative common dir is relative to cwd.
        res = run("rev_parse", ["rev-parse", "--show-toplevel", "--git-common-dir"], cwd, REV_PARSE_TIMEOUT)
        if res is None:
            return facts
        code, out = res
        lines = out.splitlines()
        if code != 0 or len(lines) != 2:
            facts["errors"].append("rev_parse:not_a_worktree")
            return facts
    top = os.path.realpath(lines[0])
    facts["toplevel"] = top
    facts["git_common_dir"] = os.path.realpath(os.path.join(cwd, lines[1]))

    res = run("branch", ["symbolic-ref", "--short", "-q", "HEAD"], top)
    if res is not None:
        code, out = res
        if code == 0 and out:
            facts["branch"] = out
        else:
            facts["errors"].append("branch:detached_or_unknown")

    res = run("head", ["rev-parse", "-q", "--verify", "HEAD"], top)
    if res is not None:
        code, out = res
        if code == 0 and out:
            facts["head"] = out
        else:
            facts["errors"].append("head:unborn_or_unknown")

    res = run("status", ["status", "--porcelain=v1", "-uno"], top)
    if res is not None:
        code, out = res
        if code == 0:
            facts["tracked_dirty"] = bool(out)
        else:
            facts["errors"].append("status:failed")
    return facts


def spawn_worker(toplevel, data_dir, env: Mapping) -> str:
    """Start a detached worker unless one is running or spawning is disabled."""
    if locks.is_locked(paths.worker_lock_path(data_dir)):
        return "running"
    if env.get("AUTOTUNE_NO_WORKER") == "1":
        return "disabled"
    python = env.get("AUTOTUNE_PYTHON") or sys.executable
    child_env = dict(env)
    child_env["AUTOTUNE_ORIGIN"] = "worker"
    try:
        _popen(
            [python, "-m", "autotune", "worker", "--project-dir", os.fspath(toplevel)],
            cwd=str(PLUGIN_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=child_env,
        )
    except Exception:
        return "spawn_failed"
    return "started"


def write_project_meta(data_dir, facts: dict) -> None:
    path = paths.project_meta_path(data_dir)
    if path.exists():
        return
    meta = {
        "project_id": paths.project_id(facts["git_common_dir"]),
        "git_common_dir": facts["git_common_dir"],
        "toplevel": facts["toplevel"],
    }
    tmp = path.with_name(f".project.json.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


class _Ack(Exception):
    def __init__(self, code: int, payload: dict):
        super().__init__(payload.get("error", ""))
        self.code = code
        self.payload = payload


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


_CWD_RAW = re.compile(rb'"cwd"\s{0,20}:\s{0,20}"([^"\\]{1,4096})"')
_ORIGIN_RAW = re.compile(rb'"origin"\s{0,20}:\s{0,20}"([a-z_]{1,32})"')


def _identity_for_error(raw: bytes, exc: "capture.EnvelopeError") -> dict | None:
    """Best-effort session/turn/cwd for an envelope that failed validation.

    Uses bounded regex scans rather than a full parse, so it stays cheap on
    oversized or deeply nested input.
    """
    ident = getattr(exc, "identity", None)
    if not ident or not ident.get("session_id"):
        return None
    m = _ORIGIN_RAW.search(raw)
    if m and m.group(1) != b"foreground":
        return None
    m = _CWD_RAW.search(raw)
    if not m:
        return None
    try:
        cwd = m.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not os.path.isabs(cwd):
        return None
    return {"cwd": cwd, "session_id": ident["session_id"], "turn_id": ident.get("turn_id")}


def _record_error_for(ident: dict, code: str, detail: str) -> None:
    """Record a capture error in the project store if the project is enrolled."""
    try:
        if not os.path.isdir(ident["cwd"]):
            return
        facts = git_facts(ident["cwd"])
        if not facts["git_common_dir"]:
            return
        cfg = config.load_config(facts["toplevel"])
        if not cfg or not cfg.get("enabled"):
            return
        data_dir = paths.project_data_dir(facts["git_common_dir"])
        _store_capture_error(data_dir, None, ident["session_id"], ident["turn_id"], code, detail, utc_now())
    except Exception:
        pass


def _store_capture_error(data_dir, conn, session_id, turn_id, code: str, detail: str, now) -> None:
    """Record a capture error in the store, or in the fallback file when the
    store cannot be written (disk full or another OS error)."""
    opened = None
    try:
        if conn is None:
            conn = opened = store.connect(paths.db_path(data_dir))
        store.record_capture_error(conn, session_id, turn_id, code, detail, now)
    except (sqlite3.Error, OSError) as exc:
        if diskcap.is_disk_full(exc):
            diskcap.append_fallback_error(data_dir, code, session_id, turn_id, now, detail)
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:
                pass


def _counts(conn) -> dict:
    c = store.queue_counts(conn)
    return {"queued": c["queued"] + c["retry_wait"], "leased": c["leased"], "blocked": c["blocked"]}


def _handle(raw: bytes, env: Mapping) -> tuple[int, dict]:
    now = utc_now()
    try:
        envelope = capture.parse_envelope(raw)
    except capture.EnvelopeError as exc:
        detail = (getattr(exc, "detail", "") or str(exc))[:200]
        ident = _identity_for_error(raw, exc)
        if ident is not None:
            _record_error_for(ident, exc.code, detail)
        return EXIT_INVALID, {"ok": False, "error": exc.code, "detail": detail}

    if envelope["origin"] != "foreground" or env.get("AUTOTUNE_ORIGIN", "foreground") != "foreground":
        return EXIT_OK, {"ok": True, "skipped": "non_foreground_origin"}

    kind = envelope["kind"]
    session_id = envelope["session_id"]
    turn_id = envelope.get("turn_id")

    if not os.path.isdir(envelope["cwd"]):
        return EXIT_OK, {"ok": True, "skipped": "not_enrolled"}
    facts = git_facts(envelope["cwd"])
    if not facts["toplevel"] or not facts["git_common_dir"]:
        if "rev_parse:not_a_worktree" in facts["errors"]:
            return EXIT_OK, {"ok": True, "skipped": "not_enrolled"}
        # A timeout or a missing git says nothing about enrollment; report it
        # so the turn is not dropped silently.
        return EXIT_PERSIST, {"ok": False, "error": "git_unavailable", "detail": ",".join(facts["errors"])[:200]}

    conn = None
    data_dir = None
    try:
        try:
            cfg = config.load_config(facts["toplevel"])
        except config.ConfigError as exc:
            raise _Ack(EXIT_PERSIST, {"ok": False, "error": "config_invalid", "detail": str(exc)[:200]})
        if cfg is None:
            return EXIT_OK, {"ok": True, "skipped": "not_enrolled"}
        if not cfg.get("enabled"):
            return EXIT_OK, {"ok": True, "skipped": "disabled"}

        data_dir = paths.project_data_dir(facts["git_common_dir"])
        write_project_meta(data_dir, facts)
        conn = store.connect(paths.db_path(data_dir))

        ack: dict = {"ok": True, "kind": kind}
        # Order activity by when the mod saw the event, not when this process ran.
        event_at = parse_iso(envelope["sent_at"])
        if kind == "turn_start":
            store.record_activity(conn, session_id, "active", turn_id, event_at)
        elif kind == "session_end":
            store.record_activity(conn, session_id, "ended", None, event_at)
        elif kind == "turn_complete":
            if diskcap.over_cap(data_dir, cfg):
                # Writing nothing keeps the store from growing; the worker
                # enforces the cap, after which turns are stored again.
                diskcap.append_fallback_error(data_dir, "disk_cap", session_id, turn_id, now)
                try:
                    store.record_activity(conn, session_id, "idle", turn_id, event_at)
                except Exception:
                    pass
                conn.close()
                conn = None
                worker = spawn_worker(facts["toplevel"], data_dir, env)
                return EXIT_PERSIST, {"ok": False, "error": "disk_cap",
                                      "detail": "project data is over max_data_mb", "worker": worker}
            repo = {k: v for k, v in facts.items()}
            episode_row, evidence_rows = capture.normalize_episode(envelope, repo, now)
            episode_row["_raw_retention_days"] = cfg["raw_retention_days"]
            # One transaction, so the ack never reports a failure for a turn
            # that was in fact stored.
            with store.transaction(conn):
                result = store.ingest_turn_complete(conn, episode_row, evidence_rows, now)
                store.record_activity(conn, session_id, "idle", turn_id, event_at)
            ack.update(
                episode_id=result["episode_id"],
                job_id=result["job_id"],
                duplicate=bool(result["duplicate"]),
            )
        else:
            raise _Ack(EXIT_INVALID, {"ok": False, "error": "invalid_field", "detail": "kind"})

        # Everything is committed here. Counts are informational, so a failure
        # reading them must not turn a stored turn into an error ack.
        try:
            counts = _counts(conn)
        except Exception:
            counts = None
        conn.close()
        conn = None
        if counts is not None:
            ack["queue"] = counts
        if kind == "turn_complete" or (counts is not None and counts["queued"] > 0):
            ack["worker"] = spawn_worker(facts["toplevel"], data_dir, env)
        return EXIT_OK, ack
    except _Ack as ack_exc:
        code, payload = ack_exc.code, ack_exc.payload
    except Exception as exc:
        code, payload = EXIT_PERSIST, {"ok": False, "error": "persistence_failed", "detail": _short(exc)}

    if data_dir is not None or payload.get("error") == "config_invalid":
        try:
            if data_dir is None:
                data_dir = paths.project_data_dir(facts["git_common_dir"])
            _store_capture_error(data_dir, conn, session_id, turn_id, payload["error"],
                                 payload.get("detail", ""), now)
        except Exception:
            pass
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return code, payload


def main(stdin: BinaryIO, stdout: TextIO, env: Mapping) -> int:
    try:
        raw = stdin.read(_IDENTITY_READ_LIMIT + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        code, payload = _handle(raw, env)
    except Exception as exc:
        code, payload = EXIT_PERSIST, {"ok": False, "error": "internal_error", "detail": _short(exc)}
    stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=False) + "\n")
    try:
        stdout.flush()
    except Exception:
        pass
    return code
