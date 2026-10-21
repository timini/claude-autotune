"""Project identity and on-disk locations for Autotune state.

All state lives outside the repository, under one directory per Git common
dir so that every worktree of a repository shares a project.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

DIR_MODE = 0o700


def project_id(git_common_dir: str) -> str:
    """Return the 16 hex char project id for a Git common dir.

    A relative path resolves against the current process cwd, so callers
    should pass an absolute path from `git rev-parse --git-common-dir`.
    """
    real = os.path.realpath(os.fspath(git_common_dir))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def base_dir() -> Path:
    override = os.environ.get("AUTOTUNE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "autotune"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def project_data_dir(git_common_dir: str) -> Path:
    base = _private_dir(base_dir())
    return _private_dir(base / project_id(git_common_dir))


def db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "autotune.sqlite3"


def log_path(data_dir: str | Path) -> Path:
    logs = _private_dir(Path(data_dir) / "logs")
    return logs / "autotune.log"


def machine_lock_path() -> Path:
    return base_dir() / "experiment.lock"


def worker_lock_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "worker.lock"


def project_meta_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "project.json"
