"""Full-harness local replay of one case on one arm (baseline or candidate).

`materialize` builds a disposable workspace under the system temp directory:
the case's task files (identical for both arms), then the harness files of the
arm's commit overlaid on top, then a local Git commit so the CLI sees a project
root. `run_arm` runs `claude -p` there with the replay profile (project setting
source only, no personal memory, stream-json so tool calls are observable).
`cleanup` removes the workspace. Grading is not done here.

Workspaces never live under the repository or the data dir, because CLAUDE.md
files in parent directories load as project memory. Every CLAUDE.md,
CLAUDE.local.md or .claude directory found in an ancestor directory is recorded;
any such finding makes the run's environment `ancestor_customization_present`.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import claude_cli, git_ops
from .config import glob_match
from .util import canonical_json

ROOT_NAME = "autotune-replay"
REPLAY_SETTINGS = {"autoMemoryEnabled": False}
ANCESTOR_FILES = ("CLAUDE.md", "CLAUDE.local.md")
ANCESTOR_DIRS = (".claude",)
EXCLUDED_GLOBS = (".autotune.json", ".autotune/**")
VERSION_TIMEOUT_SECONDS = 20.0
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_REL_CHARS = 1024
_FILE_MODE = 0o644
_EXEC_MODE = 0o755

# Error codes raised as ReplayError.code (git failures keep the GitError codes).
ERROR_CODES = ("invalid_case", "workspace_unsafe")

_cli_build_cache: dict[tuple[str, object], str | None] = {}
_git_version: list[str | None] = []


class ReplayError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class Workspace:
    path: str
    run_id: str
    root: str
    arm_commit: str
    base_commit: str
    fixture_digests: dict[str, str]
    overlaid_paths: list[str]
    harness_digests: dict[str, str]
    ancestor_customizations: list[str]
    workspace_commit: str | None = None

    @property
    def fixture_digest(self) -> str:
        """One digest over every task file; equal between arms of the same case."""
        return hashlib.sha256(canonical_json(self.fixture_digests).encode("utf-8")).hexdigest()

    @property
    def ancestor_customization_present(self) -> bool:
        return bool(self.ancestor_customizations)


@dataclass
class ArmResult:
    arm: str
    ok: bool
    error_code: str | None
    detail: str | None
    answer: str | None
    tool_uses: list[dict]
    permission_denials: list
    model: str | None
    model_requested: str | None
    usage: dict | None
    cost_usd: float | None
    duration_ms: int
    exit_code: int | None
    call_id: str | None
    cli_session_id: str | None
    harness_commit: str
    fixture_digest: str
    environment: dict = field(default_factory=dict)


# Case access: a cases.Case object or the manifest dict.


def _get(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


# Paths


def replay_root() -> Path:
    return Path(os.path.realpath(tempfile.gettempdir())) / ROOT_NAME


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _safe_rel(rel: object) -> str:
    """A relative POSIX path with no empty, '.', '..' or '.git' segment."""
    if not isinstance(rel, str) or not rel or len(rel) > _MAX_REL_CHARS or "\x00" in rel or "\\" in rel:
        raise ReplayError("invalid_case", f"invalid fixture path {rel!r}")
    if rel.startswith("/"):
        raise ReplayError("invalid_case", f"absolute fixture path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or any(p.casefold() == ".git" for p in parts):
        raise ReplayError("invalid_case", f"fixture path escapes the workspace: {rel!r}")
    return rel


def _excluded(rel: str) -> bool:
    folded = rel.casefold()
    return any(seg == ".git" for seg in folded.split("/")) or any(glob_match(g, folded) for g in EXCLUDED_GLOBS)


def _matches_any(rel: str, globs: list[str]) -> bool:
    return any(glob_match(g, rel) for g in globs)


def ancestor_customizations(path: str | Path) -> list[str]:
    """CLAUDE.md, CLAUDE.local.md and .claude directories in the ancestors of `path`."""
    found = []
    for parent in Path(os.path.realpath(path)).parents:
        for name in ANCESTOR_FILES:
            if (parent / name).exists():
                found.append(str(parent / name))
        for name in ANCESTOR_DIRS:
            if (parent / name).is_dir():
                found.append(str(parent / name))
    return found


def _write_file(ws: str, rel: str, data: bytes, mode: int) -> None:
    target = os.path.join(ws, *rel.split("/"))
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    real_parent = os.path.realpath(parent)
    if not _inside(real_parent, ws):
        raise ReplayError("invalid_case", f"fixture path escapes the workspace: {rel!r}")
    if os.path.lexists(target):
        if os.path.islink(target) or os.path.isdir(target):
            raise ReplayError("invalid_case", f"fixture path collides with a directory or link: {rel!r}")
        os.unlink(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(target, mode)


# Git reads from the user's repository (object store only, never its checkout)


def _tree_files(repo: str | Path, commit: str) -> list[tuple[str, str, str]]:
    """(path, mode, blob sha) of every regular file at `commit`; symlinks and submodules skipped."""
    proc = git_ops.run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", commit])
    files = []
    for entry in proc.stdout.split(b"\x00"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        mode, kind, sha = meta.decode("ascii").split(" ")
        if kind != "blob" or mode not in ("100644", "100755"):
            continue
        files.append((raw_path.decode("utf-8", "surrogateescape"), mode, sha))
    return files


def _blobs(repo: str | Path, shas: list[str]) -> dict[str, bytes]:
    """Blob contents for `shas` through one `git cat-file --batch`."""
    unique = list(dict.fromkeys(shas))
    if not unique:
        return {}
    proc = git_ops.run_git(repo, ["cat-file", "--batch"], stdin=("\n".join(unique) + "\n").encode("ascii"))
    out, pos, blobs = proc.stdout, 0, {}
    for sha in unique:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("ascii").split(" ")
        if len(header) != 3 or header[1] != "blob":
            raise git_ops.GitError("git_failed", f"cat-file returned {header!r} for {sha}")
        size = int(header[2])
        blobs[sha] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def _files_from_commit(repo, commit: str, globs: list[str]) -> dict[str, tuple[bytes, int]]:
    chosen = [(p, m, s) for p, m, s in _tree_files(repo, commit) if _matches_any(p, globs)]
    blobs = _blobs(repo, [s for _, _, s in chosen])
    return {p: (blobs[s], _EXEC_MODE if m == "100755" else _FILE_MODE) for p, m, s in chosen}


def _workspace_git(ws: str, args: list[str], *, stdin: bytes | None = None, hooks_off: bool = False):
    env = git_ops.git_env()
    # The workspace commit must not depend on the user's global or system Git config.
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME=git_ops.AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=git_ops.AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=git_ops.AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=git_ops.AUTHOR_EMAIL,
    )
    return git_ops.run_git(ws, args, env=env, stdin=stdin, hooks_off=hooks_off)


def _commit_workspace(ws: str, run_id: str) -> str:
    _workspace_git(ws, ["init", "-q", "--template=", "-b", "main"])
    _workspace_git(ws, ["add", "-A"])
    message = f"Autotune replay fixture {run_id}\n".encode("utf-8")
    _workspace_git(
        ws,
        ["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "--allow-empty", "-F", "-"],
        stdin=message,
        hooks_off=True,
    )
    return git_ops._out(_workspace_git(ws, ["rev-parse", "--verify", "HEAD^{commit}"]))


# Workspace lifecycle


def _check_root(root: Path, repo: str | Path, data_dir: str | Path | None) -> str:
    real_root = os.path.realpath(root)
    for other in (repo, data_dir):
        if other is None:
            continue
        real_other = os.path.realpath(other)
        if _inside(real_root, real_other) or _inside(real_other, real_root):
            raise ReplayError("workspace_unsafe", f"replay root {real_root} overlaps {real_other}")
    return real_root


def _secure_root(root: str | Path) -> None:
    """Create the replay root if needed and refuse it unless it is a real
    directory (not a symlink) owned by this user. A shared temp dir lets
    another user pre-create the root, so a group or other bit is removed."""
    root = str(root)
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        st = os.lstat(root)
    except OSError as exc:
        raise ReplayError("workspace_unsafe", f"replay root {root}: {exc}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ReplayError("workspace_unsafe", f"replay root {root} is not a real directory")
    if st.st_uid != os.getuid():
        raise ReplayError("workspace_unsafe", f"replay root {root} is owned by another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(root, 0o700)


def materialize(
    repo: str | Path,
    case,
    arm_commit: str,
    base_commit: str,
    run_id: str,
    *,
    editable_globs: list[str] | tuple[str, ...] = (),
    data_dir: str | Path | None = None,
) -> Workspace:
    """Build the workspace for one run of `case` with the harness of `arm_commit`.

    Task files come from `fixture.files` and from `fixture.from_commit.paths` at
    `base_commit` (inline files win on the same path). Any task file matching a
    harness glob (the case harness_paths plus `editable_globs`) is dropped, and
    every file matching those globs at `arm_commit` is overlaid instead, so the
    task files and their digests are the same for both arms. Evaluator files
    (`.autotune.json`, `.autotune/**`) and `.git` paths are never copied.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ReplayError("invalid_case", f"invalid run id {run_id!r}")
    harness_globs = [g for g in _list(_get(case, "harness_paths")) + list(editable_globs) if isinstance(g, str) and g]
    fixture = _get(case, "fixture") or {}
    raw_root = replay_root()
    root = _check_root(raw_root, repo, data_dir)
    _secure_root(raw_root)
    ws = os.path.join(root, run_id)
    if os.path.lexists(ws):
        _remove_tree(ws)
    os.mkdir(ws, 0o700)
    try:
        arm_sha = git_ops.rev_parse(repo, arm_commit)
        base_sha = git_ops.rev_parse(repo, base_commit)
        task: dict[str, tuple[bytes, int]] = {}
        from_commit = _get(fixture, "from_commit")
        if from_commit:
            globs = [g for g in _list(_get(from_commit, "paths")) if isinstance(g, str) and g]
            task.update(_files_from_commit(repo, base_sha, globs))
        for rel, content in (_get(fixture, "files") or {}).items():
            if not isinstance(content, str):
                raise ReplayError("invalid_case", f"fixture file {rel!r} content is not text")
            task[_safe_rel(rel)] = (content.encode("utf-8"), _FILE_MODE)
        task = {p: v for p, v in task.items() if not _excluded(p) and not _matches_any(p, harness_globs)}

        digests = {}
        for rel in sorted(task):
            data, mode = task[rel]
            _write_file(ws, _safe_rel(rel), data, mode)
            digests[rel] = hashlib.sha256(data).hexdigest()

        harness = {}
        if harness_globs:
            overlay = _files_from_commit(repo, arm_sha, harness_globs)
            for rel in sorted(overlay):
                if _excluded(rel):
                    continue
                data, mode = overlay[rel]
                _write_file(ws, _safe_rel(rel), data, mode)
                harness[rel] = hashlib.sha256(data).hexdigest()

        workspace_commit = _commit_workspace(ws, run_id)
        return Workspace(
            path=ws,
            run_id=run_id,
            root=root,
            arm_commit=arm_sha,
            base_commit=base_sha,
            fixture_digests=digests,
            overlaid_paths=sorted(harness),
            harness_digests=harness,
            ancestor_customizations=ancestor_customizations(ws),
            workspace_commit=workspace_commit,
        )
    except BaseException:
        _remove_tree(ws)
        raise


def _make_writable(func, path, *_):
    try:
        os.chmod(os.path.dirname(path), stat.S_IRWXU)
        if not os.path.islink(path):
            os.chmod(path, stat.S_IRWXU)
        func(path)
    except OSError:
        pass


def _remove_tree(path: str) -> None:
    if os.path.islink(path) or not os.path.isdir(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable)
    else:
        shutil.rmtree(path, onerror=_make_writable)


def cleanup(ws: Workspace | str | Path) -> bool:
    """Remove a workspace. Only a direct child of the replay root is ever removed;
    anything else raises ReplayError("workspace_unsafe"). False when already gone."""
    raw = ws.path if isinstance(ws, Workspace) else str(ws)
    root = os.path.realpath(replay_root())
    target = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(raw))), os.path.basename(raw))
    if os.path.dirname(target) != root or not _RUN_ID_RE.match(os.path.basename(target)):
        raise ReplayError("workspace_unsafe", f"refusing to remove {raw}: not a replay workspace")
    if not os.path.lexists(target):
        return False
    _remove_tree(target)
    return True


# Running one arm


def replay_spec(ws: Workspace, case, model: str | None) -> claude_cli.CallSpec:
    tools = [t for t in _list(_get(case, "allowed_tools")) if isinstance(t, str) and t]
    bash = [p for p in _list(_get(case, "allowed_bash")) if isinstance(p, str)]
    # dontAsk denies anything not pre-approved, so each allowed tool other than
    # Bash is approved by name and Bash only for the case's command patterns.
    patterns = [t for t in tools if t != "Bash"] + [f"Bash({p})" for p in bash]
    return claude_cli.CallSpec(
        role="replay",
        prompt=str(_get(case, "request") or ""),
        system_prompt=None,
        json_schema=None,
        cwd=ws.path,
        model=model,
        timeout_seconds=float(_get(case, "timeout_seconds") or 300),
        tools=tools,
        profile="replay",
        extra_env={"AUTOTUNE_ORIGIN": "replay"},
        allowed_tools_patterns=patterns,
        settings=dict(REPLAY_SETTINGS),
    )


def cli_build(adapter, *, job_id=None, cwd: str | None = None) -> str | None:
    """`claude --version` output (first line), cached per executable and job."""
    executable = getattr(adapter, "executable", None)
    if not executable:
        return None
    key = (executable, job_id)
    if key in _cli_build_cache:
        return _cli_build_cache[key]
    runner = getattr(adapter, "runner", None)
    if runner is None:
        return None
    spec = claude_cli.CallSpec(
        role="replay", prompt="", system_prompt=None, json_schema=None, cwd=cwd or "/", model=None,
        timeout_seconds=VERSION_TIMEOUT_SECONDS, tools=[], profile="replay",
    )
    env = claude_cli.child_env(getattr(adapter, "base_env", None) or dict(os.environ), spec, getattr(adapter, "cfg", {}) or {})
    try:
        outcome = runner([executable, "--version"], cwd=cwd or "/", env=env, stdin_text="", timeout=VERSION_TIMEOUT_SECONDS)
    except OSError:
        outcome = None
    build = None
    if outcome is not None and outcome.exit_code == 0 and not outcome.spawn_error and outcome.stdout.strip():
        build = outcome.stdout.strip().splitlines()[0][:100]
    _cli_build_cache[key] = build
    return build


def git_version() -> str | None:
    if not _git_version:
        try:
            proc = git_ops.run_git("/", ["--version"], check=False, timeout=10)
            _git_version.append(git_ops._out(proc)[:100] if proc.returncode == 0 else None)
        except git_ops.GitError:
            _git_version.append(None)
    return _git_version[0]


def environment(adapter, ws: Workspace, case, spec: claude_cli.CallSpec, *, job_id=None, model_actual=None) -> dict:
    executable = getattr(adapter, "executable", None) or "claude"
    try:
        argv = claude_cli.build_argv(executable, spec)[1:]
    except ValueError:
        argv = None
    return {
        "cli_build": cli_build(adapter, job_id=job_id, cwd=ws.path),
        "model_requested": spec.model,
        "model_actual": model_actual,
        "os": platform.platform(),
        "python": platform.python_version(),
        "git": git_version(),
        "ancestor_customizations": list(ws.ancestor_customizations),
        "ancestor_customization_present": ws.ancestor_customization_present,
        "tools": list(spec.tools),
        "allowed_bash": [p for p in _list(_get(case, "allowed_bash")) if isinstance(p, str)],
        "allowed_tools_patterns": list(spec.allowed_tools_patterns),
        "timeout_seconds": spec.timeout_seconds,
        "network": _get(case, "network"),
        "settings_flags": {
            "setting_sources": "project",
            "strict_mcp_config": True,
            "permission_mode": "dontAsk",
            "output_format": claude_cli.output_format(spec),
            "settings": spec.settings,
            "auto_memory_disabled_requested": (spec.settings or {}).get("autoMemoryEnabled") is False,
        },
        "argv": argv,
        "harness_commit": ws.arm_commit,
        "base_commit": ws.base_commit,
        "workspace_commit": ws.workspace_commit,
        "overlaid_paths": list(ws.overlaid_paths),
    }


def run_arm(
    adapter,
    ws: Workspace,
    case,
    arm: str,
    model: str | None,
    cfg: dict,
    *,
    conn_factory,
    job_id,
    attempt: int,
    cancel_event=None,
) -> ArmResult:
    """Run the case request in `ws` and return what was observed. Adapter
    errors come back as ArmResult.error_code (claude_cli.ERROR_CODES, or
    "invalid_case" when the case's tool patterns are unusable)."""
    spec = replay_spec(ws, case, model)
    env = environment(adapter, ws, case, spec, job_id=job_id)
    try:
        claude_cli.build_argv(getattr(adapter, "executable", None) or "claude", spec)
    except ValueError as exc:
        return _arm_result(arm, ws, env, model, error_code="invalid_case", detail=str(exc))
    result = adapter.call(conn_factory, spec, job_id=job_id, attempt=attempt, cancel_event=cancel_event)
    env["model_actual"] = result.model
    return ArmResult(
        arm=arm,
        ok=bool(result.ok),
        error_code=result.error_code,
        detail=result.detail,
        answer=result.text,
        tool_uses=[_tool_use(u) for u in (getattr(result, "tool_uses", None) or [])],
        permission_denials=list(result.permission_denials or []),
        model=result.model,
        model_requested=model,
        usage=result.usage,
        cost_usd=result.cost_usd,
        duration_ms=int(result.duration_ms or 0),
        exit_code=result.exit_code,
        call_id=result.call_id or None,
        cli_session_id=result.cli_session_id,
        harness_commit=ws.arm_commit,
        fixture_digest=ws.fixture_digest,
        environment=env,
    )


def _tool_use(use) -> dict:
    if not isinstance(use, dict):
        return {"name": str(use), "input_summary": "", "is_error": None}
    summary = use.get("input_summary")
    if summary is None:
        summary = canonical_json(use.get("input", {}))
    return {"name": str(use.get("name") or ""), "input_summary": str(summary)[:300], "is_error": use.get("is_error")}


def _arm_result(arm: str, ws: Workspace, env: dict, model, *, error_code: str, detail: str) -> ArmResult:
    return ArmResult(
        arm=arm, ok=False, error_code=error_code, detail=detail, answer=None, tool_uses=[], permission_denials=[],
        model=None, model_requested=model, usage=None, cost_usd=None, duration_ms=0, exit_code=None, call_id=None,
        cli_session_id=None, harness_commit=ws.arm_commit, fixture_digest=ws.fixture_digest, environment=env,
    )
