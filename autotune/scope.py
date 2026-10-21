"""Candidate scope: which changes an authored patch may contain.

A scope comes from the project policy: editable globs, executable globs,
protected globs (always protected plus `protected_paths`) and field rules for
the settings and MCP files. `validate_changes` checks a list of
`git_ops.Change` records against it and reads new content from the worktree
on disk, so the worktree must hold the revision the changes describe.

Protected paths are compared casefolded, like `config.may_match_reserved`.
Editable and executable globs are case sensitive. An executable file must be
matched by both `editable_paths` and `executable_paths`.

Field-policy files are diffed as JSON. Changed keys are dotted paths; arrays
compare whole at their key. A change is allowed when an `editable_keys` entry
is a segment prefix of it. A change that touches a key known to configure
executables (hooks, statusLine, mcpServers, env and similar), or whose new
value names a `command`, also needs an `executable_keys` entry covering it,
and every command it names must start with a repository path matched by
`executable_paths`, with no shell operators. An MCP server entry may instead
use a known interpreter (sh, bash, python, python3, node) as `command` when
`args[0]` is such a path and names a regular file. An MCP entry's `env` also
needs an `executable_keys` entry that reaches down to that `env`.

Skill, agent and command markdown under `.claude/` can run commands too: YAML
frontmatter keys such as `hooks`, `mcpServers` and `allowed-tools`, and
`!`command`` dynamic-context lines. New or changed ones are an
`executable_config` violation unless the file itself is matched by
`executable_paths`.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import config, git_ops
from .config import FIELD_POLICY_FILES, glob_match

POLICY_CODES = frozenset({"outside_scope", "config_field", "executable_config"})

KNOWN_EXECUTABLE_KEYS = {
    ".claude/settings.json": (
        "hooks", "statusLine", "subagentStatusLine", "fileSuggestion", "mcpServers", "env",
        "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh", "otelHeadersHelper",
        "enabledPlugins", "extraKnownMarketplaces", "enableAllProjectMcpServers", "enabledMcpjsonServers",
        "plugins",
    ),
    ".mcp.json": ("mcpServers",),
}
KNOWN_EXECUTABLE_KEYS[".claude/settings.local.json"] = KNOWN_EXECUTABLE_KEYS[".claude/settings.json"]

_COMMAND_STRING_KEYS = frozenset(
    {"command", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh", "otelHeadersHelper",
     "headersHelper"}
)
_MCP_INTERPRETERS = frozenset({"sh", "bash", "python", "python3", "node"})

# Markdown under these .claude directories is loaded as skills, agents or commands.
_MARKDOWN_DIRS = ("skills", "agents", "commands")
_FRONTMATTER_EXECUTABLE_KEYS = frozenset({"hooks", "mcpservers", "allowed-tools", "permissionmode"})
_DYNAMIC_CONTEXT_RE = re.compile(r"!`[^`\n]+`")
_PROJECT_DIR_PREFIXES = ("$CLAUDE_PROJECT_DIR/", "${CLAUDE_PROJECT_DIR}/")
_SHELL_META = set(";&|`$<>()\n\r\\*?[]{}~!#")


@dataclass(frozen=True)
class FieldRule:
    editable_keys: tuple[tuple[str, ...], ...]
    executable_keys: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Scope:
    editable: tuple[str, ...]
    executable: tuple[str, ...]
    protected: tuple[str, ...]
    field_rules: dict = field(default_factory=dict)


@dataclass
class ValidationResult:
    ok: bool
    violations: list[dict]
    needs_scope_change: bool

    def as_dict(self) -> dict:
        return {"ok": self.ok, "violations": self.violations, "needs_scope_change": self.needs_scope_change}


def _key_path(dotted: str) -> tuple[str, ...]:
    return tuple(dotted.split("."))


def resolve_scope(cfg: dict) -> Scope:
    rules = {}
    for name, rule in (cfg.get("field_rules") or {}).items():
        rules[name] = FieldRule(
            editable_keys=tuple(_key_path(k) for k in rule.get("editable_keys", [])),
            executable_keys=tuple(_key_path(k) for k in rule.get("executable_keys", [])),
        )
    protected = tuple(config.always_protected()) + tuple(cfg.get("protected_paths") or ())
    return Scope(
        editable=tuple(cfg.get("editable_paths") or ()),
        executable=tuple(cfg.get("executable_paths") or ()),
        protected=tuple(dict.fromkeys(protected)),
        field_rules=rules,
    )


def is_protected(path: str, scope: Scope) -> bool:
    folded = path.casefold()
    if any(seg == ".git" for seg in folded.split("/")):
        return True
    return any(glob_match(p.casefold(), folded) for p in scope.protected)


def field_policy_name(path: str) -> str | None:
    folded = path.casefold()
    for name in FIELD_POLICY_FILES:
        if folded == name.casefold():
            return name
    return None


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(glob_match(g, path) for g in globs)


def _v(code: str, path: str, detail: str) -> dict:
    return {"code": code, "path": path, "detail": detail}


def validate_changes(
    repo_or_worktree: str | Path,
    base: str,
    changes: list,
    scope: Scope,
    *,
    max_file_bytes: int = 256_000,
    git_repo: str | Path | None = None,
) -> ValidationResult:
    """`git_repo` is the repository to read `base` from; it defaults to
    `repo_or_worktree`. Pass the main repository for a candidate worktree so
    no git command follows the worktree's own `.git` file."""
    root = Path(repo_or_worktree)
    real_root = os.path.realpath(root)
    git_root = Path(git_repo) if git_repo is not None else root
    violations: list[dict] = []
    malformed = False
    for change in changes:
        found, bad = _check_change(root, real_root, git_root, base, change, scope, max_file_bytes)
        violations.extend(found)
        malformed = malformed or bad
    needs = bool(violations) and not malformed and all(v["code"] in POLICY_CODES for v in violations)
    return ValidationResult(ok=not violations, violations=violations, needs_scope_change=needs)


def _check_change(root, real_root, git_root, base, change, scope, max_file_bytes) -> tuple[list[dict], bool]:
    path = change.path
    parts = path.split("/")
    if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return [_v("path_escape", path, "path is not a clean relative path")], True
    if is_protected(path, scope):
        if change.status == "D":
            return [_v("deleted_protected", path, "protected path deleted")], True
        return [_v("protected_path", path, "protected path changed")], True
    if "160000" in (change.old_mode, change.new_mode):
        return [_v("submodule", path, "submodule change")], True

    out: list[dict] = []
    malformed = False
    policy_name = field_policy_name(path)
    if policy_name is not None:
        if policy_name not in scope.field_rules:
            out.append(_v("outside_scope", path, "settings or MCP file without field rules"))
    elif not _matches(path, scope.editable):
        out.append(_v("outside_scope", path, "not matched by editable_paths"))

    if change.status == "D":
        if policy_name is not None and policy_name in scope.field_rules:
            out.append(_v("config_field", path, "deleting a field-policy file is not allowed"))
        return out, malformed

    if change.is_symlink or change.new_mode == "120000":
        out.append(_v("symlink", path, "path is a symlink"))
        return out, True
    for i in range(1, len(parts)):
        parent = root.joinpath(*parts[:i])
        if parent.is_symlink():
            out.append(_v("symlink", path, f"parent {'/'.join(parts[:i])} is a symlink"))
            return out, True
    full = root.joinpath(*parts)
    real = os.path.realpath(full)
    if os.path.commonpath([real_root, real]) != real_root:
        out.append(_v("path_escape", path, "resolves outside the worktree"))
        return out, True

    if change.new_mode == "100755" and not (_matches(path, scope.executable) and _matches(path, scope.editable)):
        out.append(_v("executable_bit", path, "executable file not matched by executable_paths"))
        malformed = True

    try:
        st = os.lstat(full)
    except OSError:
        out.append(_v("path_escape", path, "changed file is missing from the worktree"))
        return out, True
    if st.st_size > max_file_bytes:
        out.append(_v("file_too_large", path, f"{st.st_size} bytes exceeds {max_file_bytes}"))
        return out, True
    data = full.read_bytes()
    try:
        if b"\0" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        out.append(_v("binary_file", path, "not UTF-8 text"))
        return out, True

    if policy_name is not None and policy_name in scope.field_rules:
        found, bad = _check_fields(root, git_root, base, path, policy_name, text, scope)
        out.extend(found)
        malformed = malformed or bad
    elif _is_loaded_markdown(path) and not (_matches(path, scope.executable) and _matches(path, scope.editable)):
        out.extend(_check_markdown(git_root, base, path, text))
    return out, malformed


def _is_loaded_markdown(path: str) -> bool:
    parts = path.casefold().split("/")
    if not parts[-1].endswith(".md"):
        return False
    return any(parts[i] == ".claude" and parts[i + 1] in _MARKDOWN_DIRS for i in range(len(parts) - 2))


def _frontmatter(text: str) -> dict[str, str]:
    """Top-level keys of a leading `---` YAML block, each mapped to the raw
    text of its value (including indented continuation lines). Keys are
    casefolded."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    keys: dict[str, list[str]] = {}
    current = None
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:(.*)$", line)
        if m:
            current = m.group(1).casefold()
            keys[current] = [m.group(2).strip()]
        elif current is not None:
            keys[current].append(line)
    return {k: "\n".join(v) for k, v in keys.items()}


def _check_markdown(git_root, base, path, text) -> list[dict]:
    old_bytes = git_ops.file_at(git_root, base, path)
    try:
        old = old_bytes.decode("utf-8") if old_bytes is not None else ""
    except UnicodeDecodeError:
        old = ""
    out = []
    old_front, new_front = _frontmatter(old), _frontmatter(text)
    for key in sorted(_FRONTMATTER_EXECUTABLE_KEYS & set(new_front)):
        if new_front[key] != old_front.get(key):
            out.append(_v("executable_config", path, f"frontmatter key {key} configures executables or tool permissions"))
    old_snippets = set(_DYNAMIC_CONTEXT_RE.findall(old))
    added = [m for m in _DYNAMIC_CONTEXT_RE.findall(text) if m not in old_snippets]
    if added:
        out.append(_v("executable_config", path, f"adds a shell dynamic-context command {added[0][:120]!r}"))
    return out


def _load_json(text: str):
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None


def _reject_constant(name):
    raise ValueError(name)


def _check_fields(root, git_root, base, path, policy_name, text, scope) -> tuple[list[dict], bool]:
    new = _load_json(text)
    if not isinstance(new, dict):
        return [_v("config_field", path, "not a valid JSON object")], True
    old_bytes = git_ops.file_at(git_root, base, path)
    old = None
    if old_bytes is not None:
        try:
            old = _load_json(old_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            old = None
    if not isinstance(old, dict):
        old = {}
    rule = scope.field_rules[policy_name]
    known = tuple(_key_path(k) for k in KNOWN_EXECUTABLE_KEYS.get(policy_name, ()))
    out: list[dict] = []
    for changed in changed_keys(old, new):
        dotted = ".".join(changed)
        if not any(_prefix(k, changed) for k in rule.editable_keys):
            out.append(_v("config_field", path, f"key {dotted} is not in editable_keys"))
        scan = _scan_root(changed)
        value = _get(new, scan)
        touches = any(_prefix(k, changed) or _prefix(changed, k) for k in known)
        commands = list(_commands(value, scan)) if value is not _MISSING else []
        if not touches and not commands:
            continue
        if not any(_prefix(k, changed) for k in rule.executable_keys):
            out.append(_v("executable_config", path, f"key {dotted} configures executables but is not in executable_keys"))
            continue
        for problem in _command_problems(commands, scope, root, rule):
            out.append(_v("executable_config", path, f"key {dotted}: {problem}"))
    return out, False


_MISSING = object()


def changed_keys(old, new, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Dotted key paths whose value differs; objects recurse, everything else compares whole."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[tuple[str, ...]] = []
        for key in sorted(set(old) | set(new)):
            a, b = old.get(key, _MISSING), new.get(key, _MISSING)
            if a is _MISSING and isinstance(b, dict) and b:
                a = {}
            if b is _MISSING and isinstance(a, dict) and a:
                b = {}
            if a is _MISSING or b is _MISSING:
                out.append(prefix + (key,))
            else:
                out.extend(changed_keys(a, b, prefix + (key,)))
        return out
    if old == new and type(old) is type(new):
        return []
    return [prefix]


def _scan_root(path: tuple[str, ...]) -> tuple[str, ...]:
    """Widen a change inside one MCP server entry to the whole entry, so command and args are read together."""
    if "mcpServers" in path:
        i = path.index("mcpServers")
        if len(path) > i + 2:
            return path[: i + 2]
    return path


def _prefix(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    return len(short) <= len(long) and long[: len(short)] == short


def _get(obj, path: tuple[str, ...]):
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return _MISSING
        obj = obj[key]
    return obj


def _commands(value, path: tuple[str, ...]):
    """Yield (kind, command, args) for every command named in `value`."""
    if path and path[-1] in _COMMAND_STRING_KEYS and isinstance(value, str):
        yield ("shell", value, None)
        return
    if isinstance(value, dict):
        in_mcp = "mcpServers" in path
        if in_mcp and value.get("env") and len(path) == path.index("mcpServers") + 2:
            yield ("mcp_env", path + ("env",), None)
        if in_mcp and "command" in value:
            yield ("mcp", value.get("command"), value.get("args"))
            for key, sub in value.items():
                if key not in ("command", "env"):
                    yield from _commands(sub, path + (key,))
            return
        for key, sub in value.items():
            yield from _commands(sub, path + (key,))
    elif isinstance(value, list):
        for sub in value:
            yield from _commands(sub, path)


def _command_problems(commands, scope: Scope, root=None, rule: FieldRule | None = None) -> list[str]:
    problems = []
    for kind, command, args in commands:
        if kind == "mcp":
            problem = _mcp_problem(command, args, scope, root)
        elif kind == "mcp_env":
            env_path = command
            explicit = rule is not None and any(
                _prefix(k, env_path) and len(k) >= len(env_path) for k in rule.executable_keys
            )
            problem = None if explicit else f"MCP env at {'.'.join(env_path)} needs its own executable_keys entry"
        else:
            problem = _shell_problem(command, scope)
        if problem:
            problems.append(problem)
    return problems


def _strip_project_dir(text: str) -> str:
    for p in _PROJECT_DIR_PREFIXES:
        if text.startswith(p):
            return text[len(p):]
        quoted = f'"{p[:-1]}"/'
        if text.startswith(quoted):
            return text[len(quoted):]
    return text


def approved_path(token: object, scope: Scope) -> str | None:
    """The repository path `token` names when it is an approved executable, else None."""
    if not isinstance(token, str) or not token:
        return None
    token = _strip_project_dir(token)
    if "/" not in token or token.startswith("/") or any(c in _SHELL_META for c in token):
        return None
    norm = posixpath.normpath(token)
    if norm.startswith("../") or norm == ".." or norm.startswith("/"):
        return None
    if _matches(norm, scope.executable) and _matches(norm, scope.editable) and not is_protected(norm, scope):
        return norm
    return None


def _shell_problem(command: object, scope: Scope) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return "command is not a non-empty string"
    body = _strip_project_dir(command.strip())
    if any(c in _SHELL_META for c in body):
        return f"command {command[:120]!r} uses shell operators or expansions"
    try:
        tokens = shlex.split(body)
    except ValueError:
        return f"command {command[:120]!r} cannot be parsed"
    if not tokens or approved_path(tokens[0], scope) is None:
        return f"command {command[:120]!r} does not start with a path in executable_paths"
    return None


def _mcp_problem(command: object, args: object, scope: Scope, root=None) -> str | None:
    if approved_path(command, scope) is not None:
        return None
    first = args[0] if isinstance(args, list) and args else None
    if command in _MCP_INTERPRETERS:
        script = approved_path(first, scope)
        if script is not None and _is_regular_file(root, script):
            return None
    return f"MCP command {str(command)[:120]!r} is not an approved executable path"


def _is_regular_file(root, rel: str) -> bool:
    if root is None:
        return False
    full = Path(root).joinpath(*rel.split("/"))
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(full)]) != real_root:
        return False
    try:
        return stat.S_ISREG(os.lstat(full).st_mode)
    except OSError:
        return False
