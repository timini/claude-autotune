"""On-demand background worker: lease jobs, run handlers, record outcomes.

One worker per project, guarded by the project worker lock. The worker exits
after a period with no eligible work. No SQLite transaction is held while a
handler runs; a separate thread and connection refresh the lease instead.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Callable

from . import config, diskcap, locks, paths, queue, retention, store
from .faults import fault_point  # noqa: F401  re-exported for handlers: worker.fault_point(name)
from .ingress import git_facts
from .util import iso, utc_now

RESULT_STATUSES = (
    "succeeded",
    "retry",
    "blocked_budget",
    "blocked_auth",
    "blocked_environment",
    "failed",
    "cancelled",
)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Every job type is learning dispatch or application, so pause holds all of them.
# A type listed here would keep running while paused.
PAUSE_EXEMPT_TYPES: frozenset[str] = frozenset()

RETENTION_INTERVAL_SECONDS = 3600
DEFAULT_POLL_SECONDS = 0.5

_CLASS_TO_BLOCK = {
    "auth_required": "blocked_auth",
    "budget_exhausted": "blocked_budget",
    "environment": "blocked_environment",
}

_LOG_STR_LIMIT = 200
HEARTBEAT_MAX_SECONDS = 2.0


@dataclass
class HandlerResult:
    """What a handler returns.

    `commit(conn)` runs inside the transaction that marks the job succeeded,
    only for status "succeeded" and only while this worker still owns the
    lease. If it raises, nothing is committed and the job is retried as
    commit_failed. `retry_after_seconds` turns a "retry" into a deferral of
    that length that does not use up an attempt.
    """

    status: str
    result: dict | None = None
    error_code: str | None = None
    detail: str | None = None
    commit: Callable[[sqlite3.Connection], None] | None = None
    retry_after_seconds: float | None = None


class Context:
    def __init__(
        self,
        *,
        db_path: Path,
        data_dir: Path,
        project_dir: str,
        config: dict,
        worker_id: str,
        clock: Callable[[], datetime],
        logger: "JsonLog",
        job_id: int = 0,
        attempt: int = 0,
        cancel_event: threading.Event | None = None,
        plugin_root: Path | None = None,
        claude: object | None = None,
    ) -> None:
        self.db_path = db_path
        self.data_dir = data_dir
        self.project_dir = project_dir
        self.config = config
        self.worker_id = worker_id
        self.job_id = job_id
        self.attempt = attempt
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.plugin_root = plugin_root if plugin_root is not None else PLUGIN_ROOT
        # Test hook: claude_cli.adapter_for(ctx) returns this when set.
        self.claude = claude
        self._clock = clock
        self._logger = logger

    def now(self) -> datetime:
        return self._clock()

    def log(self, event: str, **fields) -> None:
        self._logger.write(event, worker_id=self.worker_id, **fields)

    @contextmanager
    def experiment_lock(self):
        """Hold the per-machine experiment lock without waiting.

        Yields True while held. Yields False when another project's worker
        holds it; the handler should then return a retry so the job waits.
        """
        lock = locks.FileLock(paths.machine_lock_path(), blocking=False)
        held = lock.acquire()
        try:
            yield held
        finally:
            if held:
                lock.release()


class JsonLog:
    """Append-only JSON lines. Only scalar fields are kept and strings are capped,
    so payload text cannot end up in the log by accident.

    With `max_bytes`, a file that has reached that size is rotated before the
    next write: name.1 becomes name.2 and so on, keeping `keep` old files.
    """

    def __init__(
        self,
        path: Path | None,
        clock: Callable[[], datetime],
        max_bytes: int | None = None,
        keep: int = 2,
    ) -> None:
        self.path = path
        self._clock = clock
        self._max_bytes = max_bytes
        self._keep = max(0, int(keep))
        self._lock = threading.Lock()

    def _rotate(self) -> None:
        try:
            if self._max_bytes is None or os.stat(self.path).st_size < self._max_bytes:
                return
        except OSError:
            return
        base = os.fspath(self.path)
        for k in range(self._keep + 1, 0, -1):
            src = f"{base}.{k}"
            try:
                if k >= self._keep:
                    os.unlink(src)
                else:
                    os.replace(src, f"{base}.{k + 1}")
            except OSError:
                pass
        try:
            if self._keep >= 1:
                os.replace(base, f"{base}.1")
            else:
                os.unlink(base)
        except OSError:
            pass

    def write(self, event: str, **fields) -> None:
        rec = {"ts": iso(self._clock()), "event": event}
        for key, value in fields.items():
            if value is None or isinstance(value, (bool, int, float)):
                rec[key] = value
            elif isinstance(value, str):
                rec[key] = value[:_LOG_STR_LIMIT]
        if self.path is None:
            return
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
        except OSError:
            pass


Handler = Callable[[Context, sqlite3.Row], HandlerResult]

HANDLERS: dict[str, Handler] = {}


def register_handler(job_type: str, fn: Handler) -> None:
    if job_type not in queue.PRIORITIES:
        raise ValueError(f"unknown job type {job_type!r}")
    HANDLERS[job_type] = fn


def unavailable_handler(ctx: Context, job: sqlite3.Row) -> HandlerResult:
    """Placeholder until review.py registers the real handler. Used only when
    that module failed to import."""
    return HandlerResult("blocked_environment", None, "handler_unavailable",
                         f"no handler module loaded for {job['job_type']}")


register_handler("review", unavailable_handler)

# Modules that register job handlers when imported.
HANDLER_MODULES = ("review", "propose", "evaluate", "apply", "consolidate")

# (module name, error) for handler modules that failed to import; logged at worker start.
HANDLER_IMPORT_ERRORS: list[tuple[str, str]] = []
# Handler modules not present at all; logged at worker start.
HANDLER_MODULES_MISSING: list[str] = []


def _is_paused(conn) -> bool:
    value = store.get_meta(conn, "paused")
    return value is not None and value.strip().lower() in ("1", "true", "yes")


class _Heartbeat:
    """Refresh the lease on its own connection and watch for a stop request.

    Sets `cancel_event` when the job's cancel_requested_at is set, and also
    when the lease is lost, since the handler's work can no longer be recorded.
    """

    def __init__(self, db_path, job_id, worker_id, clock, lease_seconds, interval, log, cancel_event) -> None:
        self._args = (db_path, job_id, worker_id, clock, lease_seconds)
        self._interval = interval
        self._stop = threading.Event()
        self._log = log
        self.cancel_event = cancel_event
        self.lost = False
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{job_id}", daemon=True)

    def _run(self) -> None:
        db_path, job_id, worker_id, clock, lease_seconds = self._args
        conn = None
        try:
            conn = store.connect(db_path)
            while not self._stop.wait(self._interval):
                try:
                    if not queue.heartbeat(conn, job_id, worker_id, clock(), lease_seconds):
                        self.lost = True
                        self.cancel_event.set()
                        self._log("lease_lost", job_id=job_id)
                        return
                    if not self.cancel_event.is_set() and queue.cancel_requested(conn, job_id):
                        self.cancel_event.set()
                        self._log("cancel_requested", job_id=job_id)
                except sqlite3.Error as exc:
                    self._log("heartbeat_error", job_id=job_id, error=type(exc).__name__)
        except Exception as exc:
            self._log("heartbeat_error", job_id=job_id, error=type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


def _record_outcome(conn, job, worker_id, now, res: HandlerResult, max_attempts, cancelled: bool = False) -> str:
    job_id = job["id"]
    status = res.status
    if status == "succeeded":
        try:
            ok = queue.complete(conn, job_id, worker_id, now, res.result, on_commit=res.commit)
        except Exception as exc:
            if res.commit is None:
                raise
            detail = f"{type(exc).__name__}: {exc}"
            return queue.retry_later(conn, job_id, worker_id, now, "commit_failed", detail, max_attempts)
        return "succeeded" if ok else "lease_lost"
    if status == "cancelled" or cancelled:
        ok = queue.finish_cancelled(conn, job_id, worker_id, now, res.detail or res.error_code)
        return "cancelled" if ok else "lease_lost"
    if status == "retry":
        return queue.retry_later(conn, job_id, worker_id, now, res.error_code or "transient", res.detail,
                                 max_attempts, delay=res.retry_after_seconds)
    if status in store.BLOCKED_STATES:
        ok = queue.block(conn, job_id, worker_id, now, status, res.error_code or status, res.detail)
        return _final_state(conn, job_id) if ok else "lease_lost"
    ok = queue.fail(conn, job_id, worker_id, now, res.error_code or "failed", res.detail)
    return _final_state(conn, job_id) if ok else "lease_lost"


def _final_state(conn, job_id) -> str:
    """The state just recorded: block and fail turn into cancelled when a stop
    request arrived after the worker's own check."""
    row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["state"] if row is not None else "lease_lost"


def _result_from_exception(exc: BaseException) -> HandlerResult:
    klass = queue.classify_error(exc)
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = f"handler_exception:{type(exc).__name__}"
    detail = f"{type(exc).__name__}: {exc}"
    if klass == "transient":
        return HandlerResult("retry", None, code, detail)
    if klass in _CLASS_TO_BLOCK:
        return HandlerResult(_CLASS_TO_BLOCK[klass], None, code, detail)
    return HandlerResult("failed", None, code, detail)


class WorkerTerminated(BaseException):
    """Raised in the main thread on SIGTERM so `finally` blocks run: the
    running model call's process group is stopped and the lock released."""


@contextmanager
def _terminate_on_sigterm():
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise(signum, frame):
        raise WorkerTerminated(signum)

    try:
        previous = signal.signal(signal.SIGTERM, _raise)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_worker(project_dir, **kwargs) -> int:
    """Run until idle. Returns 0 on normal exit (including lock held or not enrolled),
    2 when the project cannot be resolved or its config is invalid, and 143
    when stopped by SIGTERM."""
    with _terminate_on_sigterm():
        try:
            return _run_worker(project_dir, **kwargs)
        except WorkerTerminated:
            return 143


def _run_worker(
    project_dir,
    *,
    clock: Callable[[], datetime] = utc_now,
    idle_seconds: float | None = None,
    max_jobs: int | None = None,
    handlers: dict[str, Handler] | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    heartbeat_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    facts = git_facts(project_dir)
    if not facts["toplevel"] or not facts["git_common_dir"]:
        return 2
    try:
        cfg = config.load_config(facts["toplevel"])
    except config.ConfigError:
        return 2
    if cfg is None or not cfg.get("enabled"):
        return 0

    handlers = dict(HANDLERS if handlers is None else handlers)
    data_dir = paths.project_data_dir(facts["git_common_dir"])
    db_path = paths.db_path(data_dir)
    log = JsonLog(paths.log_path(data_dir), clock, cfg["log_max_bytes"], cfg["log_keep"])

    lock = locks.FileLock(paths.worker_lock_path(data_dir), blocking=False)
    worker_id = f"w{os.getpid()}-{uuid.uuid4().hex[:8]}"
    lease_seconds = int(cfg["lease_seconds"])
    max_attempts = int(cfg["max_attempts"])
    if idle_seconds is None:
        idle_seconds = cfg["worker_idle_seconds"]
    if heartbeat_seconds is None:
        heartbeat_seconds = min(HEARTBEAT_MAX_SECONDS, lease_seconds / 3)

    jobs_done = 0
    while True:
        if not _acquire_with_retry(lock):
            return 0
        conn = None
        try:
            conn = store.connect(db_path)
            log.write("worker_start", worker_id=worker_id, pid=os.getpid())
            for module, error in HANDLER_IMPORT_ERRORS:
                log.write("handler_unavailable", worker_id=worker_id, module=module, error=error)
            for module in HANDLER_MODULES_MISSING:
                log.write("handler_module_missing", worker_id=worker_id, module=module)
            cap_state: dict = {}
            _enforce_disk_cap(conn, data_dir, cfg, clock(), log, worker_id, cap_state)
            last_retention: datetime | None = None
            last_day = None
            last_work = clock()
            while True:
                now = clock()
                if _utc_day(now) != last_day:
                    last_day = _utc_day(now)
                    _unblock_budget(conn, now, log, worker_id)
                if last_retention is None or (now - last_retention).total_seconds() >= RETENTION_INTERVAL_SECONDS:
                    last_retention = now
                    try:
                        counts = retention.apply_retention(
                            conn, now, cfg["raw_retention_days"], cfg["result_retention_days"]
                        )
                        log.write("retention", worker_id=worker_id,
                                  **{k: v for k, v in counts.items() if isinstance(v, int)})
                    except Exception as exc:
                        log.write("retention_error", worker_id=worker_id, error=type(exc).__name__)

                paused, job = False, None
                try:
                    paused = _is_paused(conn)
                    types = _dispatch_types(handlers, paused)
                    job = queue.lease(conn, worker_id, now, lease_seconds, job_types=types,
                                      max_attempts=max_attempts) if types else None
                except sqlite3.OperationalError as exc:
                    # Another process held the write lock past busy_timeout.
                    # Treat it as no work this round: poll again, and exit
                    # through the normal idle path if it never clears.
                    if not store._is_lock_error(exc):
                        raise
                    log.write("db_locked", worker_id=worker_id)

                if job is None:
                    if (clock() - last_work).total_seconds() >= idle_seconds:
                        log.write("worker_idle_exit", worker_id=worker_id, jobs=jobs_done, paused=paused)
                        break
                    sleep(poll_seconds)
                    continue

                _run_one(conn, job, handlers[job["job_type"]], worker_id, clock, cfg, facts, data_dir,
                         db_path, log, lease_seconds, max_attempts, heartbeat_seconds)
                _enforce_disk_cap(conn, data_dir, cfg, clock(), log, worker_id, cap_state)
                jobs_done += 1
                last_work = clock()
                if max_jobs is not None and jobs_done >= max_jobs:
                    log.write("worker_max_jobs_exit", worker_id=worker_id, jobs=jobs_done)
                    return 0
        finally:
            if conn is not None:
                conn.close()
            lock.release()
        # Ingress commits a job and then probes the lock. If that probe ran
        # just before the release above, ingress saw "running" and spawned no
        # one, so look once more now that the lock is free.
        if not _eligible_work(db_path, handlers, clock()):
            return 0
        log.write("worker_restart_after_release", worker_id=worker_id)


def _utc_day(now: datetime):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date()


def _unblock_budget(conn, now: datetime, log, worker_id) -> None:
    """Daily budgets reset at UTC midnight, so budget blocks from earlier days lift."""
    day_start = datetime.combine(_utc_day(now), dtime(0), tzinfo=timezone.utc)
    try:
        n = queue.unblock(conn, now, states=("blocked_budget",), updated_before=day_start)
    except sqlite3.Error as exc:
        log.write("budget_unblock_error", worker_id=worker_id, error=type(exc).__name__)
        return
    if n:
        log.write("budget_unblocked", worker_id=worker_id, jobs=n)


def _enforce_disk_cap(conn, data_dir, cfg, now: datetime, log, worker_id, state: dict) -> None:
    """Expire old evidence when the data dir is over max_data_mb.

    When a pass found nothing left to expire, later passes are skipped until
    the data grows, so an unreachable cap does not compact the database after
    every job.
    """
    try:
        if not diskcap.over_cap(data_dir, cfg):
            return
        size = diskcap.capped_bytes(data_dir)
        if state.get("exhausted_at") is not None and size <= state["exhausted_at"]:
            return
        target = int(diskcap.cap_bytes(cfg) * diskcap.ENFORCE_TARGET_FRACTION)
        target -= diskcap.usage(data_dir)["logs"]
        counts = retention.enforce_cap(conn, max(0, target), now)
        state["exhausted_at"] = None if counts["under_target"] else diskcap.capped_bytes(data_dir)
        log.write("disk_cap_enforced", worker_id=worker_id,
                  **{k: v for k, v in counts.items() if isinstance(v, (int, bool))})
    except Exception as exc:
        log.write("disk_cap_error", worker_id=worker_id, error=type(exc).__name__)


LOCK_ATTEMPTS = 5
LOCK_RETRY_SECONDS = 0.05


def _acquire_with_retry(lock: locks.FileLock) -> bool:
    """`locks.is_locked` briefly holds the lock while probing, so a single
    refused acquire may only mean ingress was probing. Retry a few times."""
    for attempt in range(LOCK_ATTEMPTS):
        if lock.acquire():
            return True
        if attempt + 1 < LOCK_ATTEMPTS:
            time.sleep(LOCK_RETRY_SECONDS)
    return False


def _dispatch_types(handlers, paused: bool) -> list[str]:
    return sorted(t for t in handlers if not paused or t in PAUSE_EXEMPT_TYPES)


def _eligible_work(db_path, handlers, now: datetime) -> bool:
    try:
        conn = store.connect(db_path)
    except Exception:
        return False
    try:
        types = _dispatch_types(handlers, _is_paused(conn))
        if not types:
            return False
        marks = ",".join("?" for _ in types)
        row = conn.execute(
            f"SELECT 1 FROM jobs WHERE state = 'queued' AND next_eligible_at <= ? AND job_type IN ({marks}) LIMIT 1",
            (iso(now), *types),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _run_one(conn, job, handler, worker_id, clock, cfg, facts, data_dir, db_path, log,
             lease_seconds, max_attempts, heartbeat_seconds) -> str:
    cancel_event = threading.Event()
    ctx = Context(
        db_path=db_path,
        data_dir=data_dir,
        project_dir=facts["toplevel"],
        config=cfg,
        worker_id=worker_id,
        clock=clock,
        logger=log,
        job_id=job["id"],
        attempt=job["attempts"],
        cancel_event=cancel_event,
    )
    job_id, job_type = job["id"], job["job_type"]
    log.write("job_start", worker_id=worker_id, job_id=job_id, job_type=job_type, attempt=job["attempts"])
    started = time.monotonic()
    hb_log = lambda event, **f: log.write(event, worker_id=worker_id, **f)  # noqa: E731
    with _Heartbeat(db_path, job_id, worker_id, clock, lease_seconds, heartbeat_seconds, hb_log, cancel_event):
        try:
            res = handler(ctx, job)
            if not isinstance(res, HandlerResult) or res.status not in RESULT_STATUSES:
                res = HandlerResult("failed", None, "invalid_handler_result", type(res).__name__)
        except Exception as exc:
            res = _result_from_exception(exc)
    try:
        # A stop requested after the last heartbeat still counts.
        cancelled = cancel_event.is_set() or queue.cancel_requested(conn, job_id)
        state = _record_outcome(conn, job, worker_id, clock(), res, max_attempts, cancelled)
    except Exception as exc:
        # The lease expires and the job is reclaimed by a later lease call.
        log.write("outcome_error", worker_id=worker_id, job_id=job_id, error=type(exc).__name__)
        state = "unrecorded"
    log.write(
        "job_end",
        worker_id=worker_id,
        job_id=job_id,
        job_type=job_type,
        state=state,
        error_code=res.error_code,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return state


def _load_handler_modules() -> None:
    """Import the handler modules, which register themselves. A module that
    fails to import (missing, or broken mid-edit) leaves its job types on the
    placeholder or unregistered instead of breaking the worker."""
    import importlib

    for name in HANDLER_MODULES:
        full = f"{__package__}.{name}"
        try:
            importlib.import_module(full)
        except ModuleNotFoundError as exc:
            # The module itself is absent (not installed in this build): its
            # job types stay queued and status shows them. A module that is
            # present but imports something missing is a broken module.
            if exc.name == full:
                HANDLER_MODULES_MISSING.append(name)
            else:
                HANDLER_IMPORT_ERRORS.append((name, f"{type(exc).__name__}: {exc}"[:200]))
        except Exception as exc:
            HANDLER_IMPORT_ERRORS.append((name, f"{type(exc).__name__}: {exc}"[:200]))


_load_handler_modules()
