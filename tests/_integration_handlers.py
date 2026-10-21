"""Worker handlers used by integration tests.

Loaded in a subprocess worker through AUTOTUNE_TEST_HANDLERS (see
cli._test_handlers) or passed directly to worker.run_worker. Side effects go
to files under $AUTOTUNE_TEST_DIR so the test process can count them.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from autotune.worker import HandlerResult


def _test_dir() -> Path:
    return Path(os.environ["AUTOTUNE_TEST_DIR"])


def _append(name: str, line: str) -> None:
    fd = os.open(_test_dir() / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def count_lines(test_dir: Path, name: str) -> list[str]:
    path = Path(test_dir) / name
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def record_review(ctx, job) -> HandlerResult:
    _append("processed", str(job["id"]))
    return HandlerResult("succeeded", {"handled_by": "tests"})


def crash_once_review(ctx, job) -> HandlerResult:
    """First run hangs (so the test can SIGKILL the worker); later runs complete."""
    starts = count_lines(_test_dir(), "starts")
    _append("starts", f"{job['id']}:{job['attempts']}")
    if not starts:
        time.sleep(60)
    _append("completions", str(job["id"]))
    return HandlerResult("succeeded", {"handled_by": "tests"})


HANDLERS = {"review": record_review}
