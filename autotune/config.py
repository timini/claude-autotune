"""Load and validate the tracked `.autotune.json` project policy.

Glob semantics (shared with later scope enforcement, see `glob_match`):
patterns are relative POSIX paths split on "/". A segment of exactly "**"
matches zero or more whole path segments. Any other segment is matched
against one path segment with fnmatch rules (`*`, `?`, `[...]`), and
wildcards do match a leading dot. Matching is case sensitive, except for
the reserved-path checks below, which casefold both sides because the default
macOS file system is case insensitive (`.GIT/hooks` is `.git/hooks` there).

Reserved paths (`.autotune.json`, `.git` and everything under it, `.autotune`
and everything under it) may not be reachable from any `editable_paths`
pattern. The check is conservative: a pattern is rejected if it could match
a reserved path, so `**/CLAUDE.md` is rejected (it covers `.git/CLAUDE.md`)
while `CLAUDE.md` and `docs/**/*.md` are accepted.

Project settings and MCP configuration (`.claude/settings.json`,
`.claude/settings.local.json`, `.mcp.json`) can add hooks and executables, so
whole-file globs over them are rejected from both path lists. They can be made
editable only through `field_rules`, which names dotted key prefixes that a
candidate may change (`editable_keys`) and the subset that may name commands
(`executable_keys`). Scope enforcement applies those rules to the parsed JSON.

`protected_paths` adds globs that no candidate may touch, on top of the
always-protected set returned by `always_protected()`.
"""

from __future__ import annotations

import copy
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

from .util import canonical_json, sha256_text

CONFIG_FILENAME = ".autotune.json"
MAX_CONFIG_BYTES = 1024 * 1024

DEFAULTS: dict = {
    "enabled": True,
    "automatic_apply": False,
    "trigger": "every_top_level_turn",
    "provider": "claude_code",
    "replay": "local",
    "editable_paths": [
        "CLAUDE.md",
        ".claude/rules/**",
        ".claude/skills/**",
        ".claude/agents/**",
    ],
    "executable_paths": [],
    "review_calls_per_day": 200,
    "experiment_calls_per_day": 50,
    "experiments_per_day": 2,
    "experiment_timeout_seconds": 1200,
    "consolidate_after_tool_calls": 250,
    "consolidate_after_skill_changes": 10,
    "raw_retention_days": 30,
    "result_retention_days": 180,
    "worker_idle_seconds": 30,
    "lease_seconds": 300,
    "max_attempts": 5,
    "reviewer_model": None,
    "author_model": None,
    "judge_model": None,
    "curator_model": None,
    "replay_model": None,
    "claude_executable": "claude",
    "allow_api_key": False,
    "review_timeout_seconds": 300,
    "author_timeout_seconds": 600,
    "review_input_chars": 60000,
    "max_data_mb": 1024,
    "log_max_bytes": 5242880,
    "log_keep": 2,
    "protected_paths": [],
    "field_rules": {},
    "pinned_skills": [],
    "critical_skills": [],
    "consolidation_overlap_threshold": 0.45,
}

_BOOL_KEYS = ("enabled", "automatic_apply", "allow_api_key")
_STRING_KEYS = ("trigger", "provider", "replay", "claude_executable")
_OPTIONAL_STRING_KEYS = ("reviewer_model", "author_model", "judge_model", "curator_model", "replay_model")
_GLOB_LIST_KEYS = ("editable_paths", "executable_paths")
_POSITIVE_INT_KEYS = (
    "review_calls_per_day",
    "experiment_calls_per_day",
    "experiments_per_day",
    "experiment_timeout_seconds",
    "consolidate_after_tool_calls",
    "consolidate_after_skill_changes",
    "raw_retention_days",
    "result_retention_days",
    "worker_idle_seconds",
    "lease_seconds",
    "max_attempts",
    "review_timeout_seconds",
    "author_timeout_seconds",
    "review_input_chars",
    "max_data_mb",
    "log_max_bytes",
    "log_keep",
)

# Skill directory globs that mark skills pinned (never merged away) or
# critical (never a merge source, never deleted). Labels only, so unlike the
# path lists they may name any path.
_SKILL_GLOB_KEYS = ("pinned_skills", "critical_skills")

_RESERVED_DIRS = (".git", ".autotune")
FIELD_POLICY_FILES = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json")
# Attribute and submodule files apply at any depth, so they are protected at any depth.
_ALWAYS_PROTECTED = (".autotune.json", ".autotune/**", ".git/**", "**/.gitmodules", "**/.gitattributes")
_FIELD_RULE_LISTS = ("editable_keys", "executable_keys")
_FIELD_KEY_RE = re.compile(r"^[A-Za-z0-9_$-]+(\.[A-Za-z0-9_$-]+)*$")
MAX_FIELD_KEYS = 50


class ConfigError(ValueError):
    pass


def always_protected() -> tuple[str, ...]:
    """Globs no candidate may touch, whatever the config says."""
    return _ALWAYS_PROTECTED


def key_covers(prefix: str, key: str) -> bool:
    """True when dotted `prefix` equals `key` or is a whole-segment prefix of it."""
    a, b = prefix.split("."), key.split(".")
    return len(a) <= len(b) and b[: len(a)] == a


def glob_match(pattern: str, path: str) -> bool:
    """Return True when relative POSIX `path` matches `pattern`."""
    return _match_segments(pattern.split("/"), path.split("/"))


def _match_segments(pat: list[str], parts: list[str]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(_match_segments(rest, parts[i:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatchcase(parts[0], head) and _match_segments(rest, parts[1:])


def may_match_reserved(pattern: str) -> bool:
    """Return True when `pattern` could match a reserved path, ignoring case."""
    pattern = pattern.casefold()
    if glob_match(pattern, CONFIG_FILENAME):
        return True
    if any(glob_match(pattern, name) for name in _RESERVED_DIRS):
        return True
    segs = pattern.split("/")
    first = segs[0]
    if first == "**":
        return True
    if len(segs) > 1 and any(fnmatchcase(name, first) for name in _RESERVED_DIRS):
        return True
    return False


def may_match_field_policy_file(pattern: str) -> bool:
    """Return True when `pattern` could match a settings or MCP file, ignoring case."""
    pattern = pattern.casefold()
    return pattern.split("/")[0] == "**" or any(glob_match(pattern, path) for path in FIELD_POLICY_FILES)


def validate_glob(pattern: object, key: str) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError(f"{key}: entries must be non-empty strings")
    if "\\" in pattern or "\x00" in pattern:
        raise ConfigError(f"{key}: {pattern!r} must use POSIX separators")
    if pattern.startswith("/"):
        raise ConfigError(f"{key}: {pattern!r} must be relative")
    if pattern.endswith("/"):
        raise ConfigError(f"{key}: {pattern!r} must not end with '/'")
    for seg in pattern.split("/"):
        if seg == "":
            raise ConfigError(f"{key}: {pattern!r} has an empty segment")
        if seg in (".", ".."):
            raise ConfigError(f"{key}: {pattern!r} has a '{seg}' segment")
    return pattern


def _validate(cfg: dict) -> None:
    unknown = sorted(set(cfg) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    for key in _BOOL_KEYS:
        if type(cfg[key]) is not bool:
            raise ConfigError(f"{key}: must be true or false")
    for key in _STRING_KEYS:
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ConfigError(f"{key}: must be a non-empty string")
    for key in _POSITIVE_INT_KEYS:
        value = cfg[key]
        if type(value) is not int or value < 1:
            raise ConfigError(f"{key}: must be a positive integer")
    for key in _GLOB_LIST_KEYS:
        value = cfg[key]
        if not isinstance(value, list):
            raise ConfigError(f"{key}: must be a list of glob strings")
        for pattern in value:
            validate_glob(pattern, key)
    for key in _GLOB_LIST_KEYS:
        for pattern in cfg[key]:
            if may_match_reserved(pattern):
                raise ConfigError(f"{key}: {pattern!r} could match a reserved path")
            if may_match_field_policy_file(pattern):
                raise ConfigError(
                    f"{key}: {pattern!r} could match a settings or MCP file; "
                    "use field-level rules (field_rules) to make those files editable"
                )
    for key in _OPTIONAL_STRING_KEYS:
        value = cfg[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"{key}: must be a non-empty string or null")
    if not isinstance(cfg["protected_paths"], list):
        raise ConfigError("protected_paths: must be a list of glob strings")
    for pattern in cfg["protected_paths"]:
        validate_glob(pattern, "protected_paths")
    for key in _SKILL_GLOB_KEYS:
        if not isinstance(cfg[key], list):
            raise ConfigError(f"{key}: must be a list of glob strings")
        for pattern in cfg[key]:
            validate_glob(pattern, key)
    threshold = cfg["consolidation_overlap_threshold"]
    if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
        raise ConfigError("consolidation_overlap_threshold: must be a number from 0 to 1")
    _validate_field_rules(cfg["field_rules"])


def _validate_field_rules(rules: object) -> None:
    if not isinstance(rules, dict):
        raise ConfigError("field_rules: must be an object keyed by settings or MCP file path")
    for path, rule in rules.items():
        where = f"field_rules[{path!r}]"
        if path not in FIELD_POLICY_FILES:
            raise ConfigError(f"{where}: file must be one of {', '.join(FIELD_POLICY_FILES)}")
        if not isinstance(rule, dict):
            raise ConfigError(f"{where}: must be an object")
        unknown = sorted(set(rule) - set(_FIELD_RULE_LISTS))
        if unknown:
            raise ConfigError(f"{where}: unknown keys: {', '.join(unknown)}")
        lists = {}
        for name in _FIELD_RULE_LISTS:
            value = rule.get(name, [])
            if not isinstance(value, list):
                raise ConfigError(f"{where}.{name}: must be a list of dotted keys")
            if len(value) > MAX_FIELD_KEYS:
                raise ConfigError(f"{where}.{name}: at most {MAX_FIELD_KEYS} entries")
            for item in value:
                if not isinstance(item, str) or not _FIELD_KEY_RE.fullmatch(item):
                    raise ConfigError(f"{where}.{name}: {item!r} is not a dotted key")
            lists[name] = value
        for item in lists["executable_keys"]:
            if not any(key_covers(prefix, item) for prefix in lists["editable_keys"]):
                raise ConfigError(f"{where}.executable_keys: {item!r} is not covered by editable_keys")


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ConfigError(f"duplicate key: {k}")
        out[k] = v
    return out


def _reject_constant(name: str):
    raise ConfigError(f"invalid JSON constant: {name}")


def parse_config(text: str) -> dict:
    """Parse and validate config text, returning a merged copy of DEFAULTS."""
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from None
    if not isinstance(data, dict):
        raise ConfigError("top level must be a JSON object")
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(copy.deepcopy(data))
    _validate(cfg)
    return cfg


def load_config(toplevel: str | Path) -> dict | None:
    path = Path(toplevel) / CONFIG_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {CONFIG_FILENAME}: {exc.strerror or exc}") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{CONFIG_FILENAME} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{CONFIG_FILENAME} is not valid UTF-8") from None
    return parse_config(text)


def config_digest(cfg: dict) -> str:
    return sha256_text(canonical_json(cfg))
