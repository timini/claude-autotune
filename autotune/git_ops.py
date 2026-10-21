"""Git operations for candidate worktrees.

Every call uses an argv list with an explicit `cwd`, `GIT_TERMINAL_PROMPT=0`
and `LC_ALL=C`. Commands that could run hooks get `-c core.hooksPath=/dev/null`
so neither user hooks nor hooks written by a candidate run from Autotune.
Diff commands pass `--no-ext-diff --no-textconv` so no configured diff driver
runs either. Failures raise `GitError` with code `git_unavailable` (git could
not be started), `git_failed` (non-zero exit) or `git_timeout`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
BRANCH_PREFIX = "autotune/"
TRAILER_KEY = "Autotune-Candidate"
AUTHOR_NAME = "Autotune"
AUTHOR_EMAIL = "autotune@localhost.invalid"

_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class GitError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    old_mode: str
    new_mode: str
    is_symlink: bool


def git_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    # Read-only commands such as `git status` must not refresh the user's index.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(
    cwd: str | Path,
    args: list[str],
    *,
    hooks_off: bool = False,
    stdin: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    env: dict | None = None,
    git_dir: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """Run git in `cwd`. With `git_dir`, git uses that repository directory and
    `cwd` as the work tree, and never reads a `.git` file inside `cwd`."""
    argv = ["git", *(_NO_HOOKS if hooks_off else ()), *args]
    if not os.path.isdir(cwd):
        raise GitError("git_failed", f"not a directory: {cwd}")
    env = dict(env) if env is not None else git_env()
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
        env["GIT_WORK_TREE"] = str(cwd)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=timeout,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise GitError("git_unavailable", str(exc)) from None
    except subprocess.TimeoutExpired:
        raise GitError("git_timeout", f"git {args[0] if args else ''} exceeded {timeout:g}s") from None
    if check and proc.returncode != 0:
        raise GitError("git_failed", _short(args, proc))
    return proc


def _short(args: list[str], proc: subprocess.CompletedProcess) -> str:
    err = proc.stderr.decode("utf-8", "replace").strip()[:300]
    return f"git {' '.join(args[:2])} exited {proc.returncode}: {err}"


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def head(repo: str | Path) -> str:
    return _out(run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]))


def rev_parse(repo: str | Path, rev: str) -> str:
    return _out(run_git(repo, ["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"]))


def current_branch(repo: str | Path) -> str | None:
    proc = run_git(repo, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitError("git_failed", _short(["symbolic-ref"], proc))
    return _out(proc) or None


def is_ancestor(repo: str | Path, a: str, b: str) -> bool:
    proc = run_git(repo, ["merge-base", "--is-ancestor", a, b], check=False)
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitError("git_failed", _short(["merge-base", "--is-ancestor"], proc))


def branch_exists(repo: str | Path, branch: str) -> bool:
    proc = run_git(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    return proc.returncode == 0


def dirty_paths(repo: str | Path, pathspecs: list[str] | None = None) -> list[str]:
    """Staged, unstaged and untracked paths, relative to the repository root."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    if pathspecs:
        args += ["--", *(f":(top,glob){p}" for p in pathspecs)]
    raw = run_git(repo, args).stdout.decode("utf-8", "surrogateescape")
    fields = raw.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in "RC" or status[1] in "RC":
            if i < len(fields) and fields[i]:
                paths.append(fields[i])
            i += 1
    return sorted(set(paths))


def _check_branch(branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX) or not _ID_RE.match(branch[len(BRANCH_PREFIX):]):
        raise ValueError(f"refusing branch {branch!r}: Autotune only manages {BRANCH_PREFIX}<id> branches")


def create_worktree(repo: str | Path, branch: str, base: str, path: str | Path) -> None:
    _check_branch(branch)
    if branch_exists(repo, branch):
        raise GitError("git_failed", f"branch {branch} already exists")
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise GitError("git_failed", f"worktree path {target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(repo, ["worktree", "add", "-q", "-b", branch, "--end-of-options", str(target), base], hooks_off=True)


def common_dir(repo: str | Path) -> Path:
    return Path(_out(run_git(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"])))


def worktree_admin_dir(repo: str | Path, worktree: str | Path) -> Path:
    """The repository's own admin directory for `worktree`
    (`<common dir>/worktrees/<name>`), found from the main repository only.

    Raises GitError when no admin directory points back at `worktree`.
    """
    target = os.path.realpath(worktree)
    admin_root = common_dir(repo) / "worktrees"
    try:
        entries = sorted(admin_root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if _admin_points_at(entry, target):
            return entry
    raise GitError("git_failed", f"no worktree admin directory for {target}")


def _admin_points_at(admin: Path, target: str) -> bool:
    try:
        gitdir = (admin / "gitdir").read_text(encoding="utf-8", errors="surrogateescape").strip()
    except OSError:
        return False
    return os.path.realpath(os.path.dirname(gitdir)) == target if gitdir else False


def worktree_paths(repo: str | Path) -> list[Path]:
    raw = run_git(repo, ["worktree", "list", "--porcelain", "-z"]).stdout.decode("utf-8", "surrogateescape")
    return [Path(f[len("worktree "):]) for f in raw.split("\0") if f.startswith("worktree ")]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def remove_worktree(
    repo: str | Path,
    path: str | Path,
    *,
    delete_branch: str | None,
    worktrees_root: str | Path,
) -> None:
    """Remove an Autotune worktree and optionally its `autotune/...` branch.

    The path must lie under `worktrees_root` (the data dir's worktrees
    directory) and must not be the main checkout. Missing worktrees and
    branches are tolerated so cleanup can be repeated. Only this worktree's
    registration is touched: no repository-wide `git worktree prune`.
    """
    target = Path(os.path.realpath(path))
    root = Path(os.path.realpath(worktrees_root))
    if not _inside(target, root):
        raise ValueError(f"refusing to remove {target}: not under {root}")
    if delete_branch is not None:
        _check_branch(delete_branch)
    registered = [Path(os.path.realpath(p)) for p in worktree_paths(repo)]
    if registered and target == registered[0]:
        raise ValueError(f"refusing to remove the main worktree {target}")
    if target in registered and target.is_dir() and not target.is_symlink():
        run_git(repo, ["worktree", "remove", "--force", "--force", str(target)], hooks_off=True)
    else:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        _drop_admin_dir(repo, target)
    if delete_branch is not None and branch_exists(repo, delete_branch):
        run_git(repo, ["branch", "-D", "--end-of-options", delete_branch], hooks_off=True)


def _drop_admin_dir(repo: str | Path, target: Path) -> None:
    """Delete the stale registration of `target` only."""
    admin = common_dir(repo) / "worktrees" / target.name
    if admin.is_dir() and not admin.is_symlink() and _admin_points_at(admin, str(target)):
        shutil.rmtree(admin)


def _parse_raw(raw: bytes) -> list[Change]:
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    changes: list[Change] = []
    i = 0
    while i + 1 < len(fields):
        meta, path = fields[i], fields[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        old_mode, new_mode, _old_sha, _new_sha, status = meta[1:].split(" ")
        status = status[0]
        changes.append(Change(path, status, old_mode, new_mode, new_mode == "120000"))
    return changes


def changed_files(repo: str | Path, base: str, rev_or_worktree: str | Path) -> list[Change]:
    """Changes from `base` to a revision, or to a worktree's current files.

    A `Path` or absolute directory argument is treated as a worktree: all of its edits, including
    untracked and ignored files, are staged first so nothing escapes review. Git is pointed at the
    worktree's admin directory in `repo`, so a rewritten `.git` file in the worktree is not followed.
    """
    diff = ["diff", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none"]
    wt = Path(rev_or_worktree)
    if isinstance(rev_or_worktree, Path) or (os.path.isabs(rev_or_worktree) and wt.is_dir()):
        admin = None if os.path.realpath(wt) == os.path.realpath(repo) else worktree_admin_dir(repo, wt)
        run_git(wt, ["add", "-A", "--force", "--", ":/"], git_dir=admin)
        proc = run_git(wt, [*diff, "--cached", base, "--"], git_dir=admin)
    else:
        proc = run_git(repo, [*diff, base, str(rev_or_worktree), "--"])
    return sorted(_parse_raw(proc.stdout), key=lambda c: c.path)


def _check_id(candidate_id: str) -> None:
    if not _ID_RE.match(candidate_id):
        raise ValueError(f"invalid candidate id {candidate_id!r}")


def commit_candidate(worktree: str | Path, message: str, candidate_id: str, *, repo: str | Path | None = None) -> str:
    """Commit the staged changes in `worktree` and return the new commit SHA.

    With `repo`, git uses the worktree's admin directory in `repo` instead of
    the worktree's own `.git` file.
    """
    _check_id(candidate_id)
    admin = worktree_admin_dir(repo, worktree) if repo is not None else None
    proc = run_git(worktree, ["diff", "--cached", "--quiet", "--ignore-submodules=none"], check=False, git_dir=admin)
    if proc.returncode == 0:
        raise GitError("git_failed", "nothing staged to commit")
    body = message.strip() or "Autotune candidate"
    text = f"{body}\n\n{TRAILER_KEY}: {candidate_id}\n"
    env = git_env()
    env.update(
        GIT_AUTHOR_NAME=AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=AUTHOR_EMAIL,
    )
    args = [
        "-c", "commit.gpgsign=false",
        "-c", f"user.name={AUTHOR_NAME}",
        "-c", f"user.email={AUTHOR_EMAIL}",
        "commit", "-q", "--no-verify", "--cleanup=verbatim", "-F", "-",
    ]
    run_git(worktree, args, hooks_off=True, stdin=text.encode("utf-8"), env=env, git_dir=admin)
    return _out(run_git(worktree, ["rev-parse", "--verify", "HEAD^{commit}"], git_dir=admin))


def _has_trailer(body: str, candidate_id: str) -> bool:
    paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if not paragraphs:
        return False
    wanted = f"{TRAILER_KEY}: {candidate_id}"
    return any(line.strip() == wanted for line in paragraphs[-1].splitlines())


def find_trailer_commits(repo: str | Path, candidate_id: str, refs: str | list[str] = "--all") -> list[str]:
    """Commits whose final message paragraph carries this candidate's trailer."""
    _check_id(candidate_id)
    ref_args = [refs] if isinstance(refs, str) else list(refs)
    proc = run_git(
        repo,
        ["log", *ref_args, f"--grep={TRAILER_KEY}: {candidate_id}", "--fixed-strings", "--format=%H%x00%B%x1e", "--"],
    )
    found: list[str] = []
    for record in proc.stdout.decode("utf-8", "replace").split("\x1e"):
        record = record.lstrip("\n")
        if "\0" not in record:
            continue
        sha, body = record.split("\0", 1)
        if _has_trailer(body, candidate_id) and sha not in found:
            found.append(sha)
    return found


def file_at(repo: str | Path, rev: str, path: str) -> bytes | None:
    """Blob content of `path` at `rev`, or None when it is absent or not a file."""
    spec = f"{rev}:{path}"
    proc = run_git(repo, ["cat-file", "-t", spec], check=False)
    if proc.returncode != 0 or _out(proc) != "blob":
        return None
    return run_git(repo, ["cat-file", "blob", spec]).stdout


def diff_text(repo: str | Path, base: str, rev: str, max_bytes: int = 200_000) -> str:
    proc = run_git(
        repo,
        ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", base, rev, "--"],
    )
    data = proc.stdout
    if len(data) <= max_bytes:
        return data.decode("utf-8", "replace")
    return data[:max_bytes].decode("utf-8", "ignore") + f"\n[diff truncated at {max_bytes} bytes]\n"


# Application and revert (cycle 2)

REVERT_TRAILER_KEY = "Autotune-Revert"


@dataclass(frozen=True)
class StatusEntry:
    xy: str
    path: str

    @property
    def untracked(self) -> bool:
        return self.xy == "??"

    @property
    def ignored(self) -> bool:
        return self.xy == "!!"


def status_entries(repo: str | Path) -> list[StatusEntry]:
    """`git status` entries with their XY codes. Untracked files are listed
    individually; ignored files are not listed."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    fields = run_git(repo, args).stdout.decode("utf-8", "surrogateescape").split("\0")
    out: list[StatusEntry] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        out.append(StatusEntry(xy, path))
        if xy[0] in "RC" or xy[1] in "RC":
            if i < len(fields) and fields[i]:
                out.append(StatusEntry(xy, fields[i]))
            i += 1
    return out


def tracked_changes(repo: str | Path) -> list[str]:
    """Paths with a staged or unstaged change to tracked content (anything but untracked)."""
    return sorted({e.path for e in status_entries(repo) if not e.untracked and not e.ignored})


def parent_of(repo: str | Path, rev: str) -> str:
    return rev_parse(repo, f"{rev}^")


def is_ancestor_or_equal(repo: str | Path, ancestor: str, descendant: str) -> bool:
    """True when `descendant` is `ancestor` or has it in its history. Unknown
    revisions count as not descending."""
    proc = run_git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    return proc.returncode == 0


def branch_head(repo: str | Path, branch: str) -> str | None:
    proc = run_git(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], check=False)
    return _out(proc) or None if proc.returncode == 0 else None


def worktree_matches(repo: str | Path, commit: str) -> bool:
    """HEAD is `commit` and neither the index nor tracked files differ from it."""
    if head(repo) != rev_parse(repo, commit):
        return False
    diff = ["diff", "--quiet", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none"]
    if run_git(repo, [*diff, "--cached", commit, "--"], check=False).returncode != 0:
        return False
    return run_git(repo, [*diff, commit, "--"], check=False).returncode == 0


def merge_ff_only(repo: str | Path, commit: str) -> str:
    """Fast-forward the checked-out branch to `commit` with hooks disabled.

    Git refuses (GitError git_failed) when HEAD is not an ancestor of `commit`
    or when the merge would overwrite local changes; nothing is changed then.
    Local changes are never stashed, whatever the user's merge.autoStash.
    Returns the new HEAD.
    """
    # merge.autoStash would stash local changes and leave conflict markers
    # instead of refusing, so it is turned off for this merge.
    run_git(repo, ["-c", "merge.autoStash=false", "merge", "--ff-only", "--no-autostash", "--no-edit", "--no-verify",
                   "-q", "--end-of-options", commit],
            hooks_off=True)
    return head(repo)


def find_trailer_commits_for(repo: str | Path, key: str, candidate_id: str,
                             refs: str | list[str] = "--all") -> list[str]:
    """Like find_trailer_commits, for any trailer key (Autotune-Candidate or Autotune-Revert)."""
    _check_id(candidate_id)
    ref_args = [refs] if isinstance(refs, str) else list(refs)
    proc = run_git(repo, ["log", *ref_args, f"--grep={key}: {candidate_id}", "--fixed-strings",
                          "--format=%H%x00%B%x1e", "--"])
    wanted = f"{key}: {candidate_id}"
    found: list[str] = []
    for record in proc.stdout.decode("utf-8", "replace").split("\x1e"):
        record = record.lstrip("\n")
        if "\0" not in record:
            continue
        sha, body = record.split("\0", 1)
        paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
        if paragraphs and any(line.strip() == wanted for line in paragraphs[-1].splitlines()) and sha not in found:
            found.append(sha)
    return found


class RevertConflict(GitError):
    def __init__(self, detail: str):
        super().__init__("revert_conflict", detail)


def _autotune_identity_env() -> dict:
    env = git_env()
    env.update(
        GIT_AUTHOR_NAME=AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=AUTHOR_EMAIL,
    )
    return env


def revert_in_worktree(repo: str | Path, worktree: str | Path, commit: str, candidate_id: str) -> str:
    """In an Autotune worktree, revert `commit` and record it as one commit whose
    message ends with the `Autotune-Revert: <id>` trailer. Returns the new SHA.

    On a conflict the revert is aborted, the worktree is left at its starting
    commit, and RevertConflict is raised. Hooks never run.
    """
    _check_id(candidate_id)
    admin = worktree_admin_dir(repo, worktree)
    env = _autotune_identity_env()
    flags = ["-c", "commit.gpgsign=false", "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}"]
    proc = run_git(worktree, [*flags, "revert", "--no-edit", "--no-commit", "--end-of-options", commit],
                   hooks_off=True, env=env, git_dir=admin, check=False)
    if proc.returncode != 0:
        detail = _short(["revert", commit], proc)
        run_git(worktree, ["revert", "--abort"], hooks_off=True, env=env, git_dir=admin, check=False)
        run_git(worktree, ["reset", "-q", "--hard", "HEAD"], hooks_off=True, env=env, git_dir=admin, check=False)
        raise RevertConflict(detail)
    if run_git(worktree, ["diff", "--cached", "--quiet"], git_dir=admin, check=False).returncode == 0:
        raise GitError("git_failed", f"reverting {commit} changes nothing")
    subject = run_git(repo, ["log", "-1", "--format=%s", commit]).stdout.decode("utf-8", "replace").strip()
    text = (f'Revert "{subject}"\n\nThis reverts commit {commit}.\n\n'
            f"{REVERT_TRAILER_KEY}: {candidate_id}\n")
    run_git(worktree, [*flags, "commit", "-q", "--no-verify", "--cleanup=verbatim", "-F", "-"],
            hooks_off=True, stdin=text.encode("utf-8"), env=env, git_dir=admin)
    return _out(run_git(worktree, ["rev-parse", "--verify", "HEAD^{commit}"], git_dir=admin))
