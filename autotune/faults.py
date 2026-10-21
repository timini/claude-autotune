"""Test-only crash points for application and revert recovery tests.

`fault_point(name)` ends the process at once with exit code 70 when the
environment names that point in AUTOTUNE_TEST_FAULT. It is honoured only when
AUTOTUNE_HOME is set (so real data is never involved) and
AUTOTUNE_TEST_FAULTS_ENABLED is "1". Otherwise it does nothing. `os._exit`
skips `finally` blocks, atexit handlers and buffered writes, which is what a
killed worker looks like to the next one.
"""

from __future__ import annotations

import os
from typing import Mapping

FAULT_EXIT_CODE = 70

FAULT_POINTS = frozenset({
    "apply_before_merge",
    "apply_after_merge",
    "apply_after_record",
    "revert_after_commit",
    "revert_after_merge",
})


def enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("AUTOTUNE_HOME")) and env.get("AUTOTUNE_TEST_FAULTS_ENABLED") == "1"


def armed(name: str, env: Mapping[str, str] | None = None) -> bool:
    """True when `name` is the configured fault point and faults are enabled.
    AUTOTUNE_TEST_FAULT may name several points separated by commas."""
    if name not in FAULT_POINTS:
        raise ValueError(f"unknown fault point {name!r}")
    env = os.environ if env is None else env
    if not enabled(env):
        return False
    wanted = {p.strip() for p in (env.get("AUTOTUNE_TEST_FAULT") or "").split(",")}
    return name in wanted


def fault_point(name: str, env: Mapping[str, str] | None = None, *, _exit=os._exit) -> None:
    if armed(name, env):
        _exit(FAULT_EXIT_CODE)
