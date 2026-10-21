"""Real temporary Git repositories for git_ops and scope tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def git(repo, *args, stdin: bytes = b"") -> str:
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("utf-8", "surrogateescape")


def write(root: Path, rel: str, content, mode: int | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)
    return path


def snapshot(repo: Path) -> tuple:
    """Hash every file outside .git, plus status and HEAD, to prove a checkout was not touched."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(repo).as_posix()
            data = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
            files[rel] = (hashlib.sha256(data).hexdigest(), os.lstat(p).st_mode)
    index = (repo / ".git" / "index").read_bytes()
    return files, git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"), git(repo, "rev-parse", "HEAD"), hashlib.sha256(index).hexdigest()


class GitRepoTestCase(unittest.TestCase):
    """A repo at self.repo with a commit on main, live hooks and isolated git config."""

    base_files: dict = {
        "CLAUDE.md": "# Project rules\n",
        "src/app.py": "print('hi')\n",
        ".gitignore": "build/\n",
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "User",
                "GIT_AUTHOR_EMAIL": "user@example.invalid",
                "GIT_COMMITTER_NAME": "User",
                "GIT_COMMITTER_EMAIL": "user@example.invalid",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        for rel, content in self.base_files.items():
            write(self.repo, rel, content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.marker = self.tmp / "hook-ran"
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        for name in ("pre-commit", "commit-msg", "post-commit", "post-checkout", "prepare-commit-msg", "reference-transaction"):
            write(hooks, name, f"#!/bin/sh\necho {name} >> '{self.marker}'\n", 0o755)
        git(self.repo, "config", "core.hooksPath", str(hooks))
        self.worktrees = self.tmp / "data" / "worktrees"
