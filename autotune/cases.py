"""Replay case manifests: loading from the base commit, selection, evaluator identity.

Cases live at `.autotune/cases/<id>.json` and are always read from a commit
(`git ls-tree` and `git cat-file`), never from the working tree, so the cases
used for a candidate are the committed ones. Invalid cases are reported as
error entries {"path", "code", "detail"} and never used.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, config, git_ops, jsonschema_lite

CASES_DIR = ".autotune/cases"
EVALUATOR_DIR = ".autotune"
MAX_CASE_BYTES = 1024 * 1024
MAX_FIXTURE_FILES = 200
MAX_FIXTURE_BYTES = 1024 * 1024
DEFAULT_COMMAND_TIMEOUT = 120
KIND_ORDER = ("target", "generalization", "protected", "negative_trigger", "general")

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _PLUGIN_ROOT / "schemas" / "case.schema.json"
_SCHEMA_CACHE: dict = {}


@dataclass
class Case:
    id: str
    task_family: str
    kind: str
    critical: bool
    description: str
    request: str
    fixture: dict                 # {"files": {path: content}, "from_commit": None | {"paths": [...]}}
    harness_paths: list[str]
    allowed_tools: list[str]
    allowed_bash: list[str]
    network: str
    trials_per_arm: int
    timeout_seconds: int
    graders: list[dict]           # normalized: every grader has id, type, critical and its defaults
    judge: dict | None            # None or {"rubric", "required"}
    path: str = ""                # repository path the case was read from
    digest: str = ""              # sha256 hex of the committed file bytes
    raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_family": self.task_family,
            "kind": self.kind,
            "critical": self.critical,
            "description": self.description,
            "request": self.request,
            "fixture": self.fixture,
            "harness_paths": list(self.harness_paths),
            "allowed_tools": list(self.allowed_tools),
            "allowed_bash": list(self.allowed_bash),
            "network": self.network,
            "trials_per_arm": self.trials_per_arm,
            "timeout_seconds": self.timeout_seconds,
            "graders": [dict(g) for g in self.graders],
            "judge": dict(self.judge) if self.judge else None,
        }


def schema() -> dict:
    if "schema" not in _SCHEMA_CACHE:
        loaded = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        loaded.pop("$schema", None)
        _SCHEMA_CACHE["schema"] = loaded
    return _SCHEMA_CACHE["schema"]


def _err(path: str, code: str, detail: str) -> dict:
    return {"path": path, "code": code, "detail": detail[:500]}


# Parsing and validation


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_json(data: bytes):
    text = data.decode("utf-8")
    return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)


def _path_problem(value: str, label: str) -> str | None:
    """Reasons a relative POSIX path or glob is unsafe, mirroring config.validate_glob."""
    try:
        config.validate_glob(value, label)
    except config.ConfigError as exc:
        return str(exc)
    if any(seg.casefold() == ".git" for seg in value.split("/")):
        return f"{label}: {value!r} is inside .git"
    return None


def _literal_path_problem(value: str, label: str) -> str | None:
    problem = _path_problem(value, label)
    if problem is None and any(ch in value for ch in "*?["):
        problem = f"{label}: {value!r} must be a literal path, not a glob"
    return problem


def _default_grader_id(grader: dict, index: int) -> str:
    return f"{grader['type'].replace('_', '-')}-{index + 1}"


def _normalize_grader(grader: dict, index: int, case_critical: bool) -> dict:
    g = dict(grader)
    g.setdefault("id", _default_grader_id(g, index))
    g.setdefault("critical", case_critical)
    kind = g["type"]
    if kind in ("answer_contains", "answer_not_contains"):
        g.setdefault("case_sensitive", False)
    if kind == "command":
        g.setdefault("expect_exit", 0)
        g.setdefault("test_format", None)
        g.setdefault("min_tests", 1 if g["test_format"] else None)
        g.setdefault("timeout_seconds", DEFAULT_COMMAND_TIMEOUT)
    return g


def _semantic_problems(obj: dict, graders: list[dict]) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    fixture = obj.get("fixture") or {}
    files = fixture.get("files") or {}
    from_commit = fixture.get("from_commit")
    if len(files) > MAX_FIXTURE_FILES:
        problems.append(("fixture", f"fixture has {len(files)} files; at most {MAX_FIXTURE_FILES}"))
    total = sum(len(v.encode("utf-8")) for v in files.values())
    if total > MAX_FIXTURE_BYTES:
        problems.append(("fixture", f"fixture files total {total} bytes; at most {MAX_FIXTURE_BYTES}"))
    for rel in files:
        problem = _literal_path_problem(rel, "fixture.files")
        if problem:
            problems.append(("unsafe_path", problem))
    for rel in (from_commit or {}).get("paths", []):
        problem = _path_problem(rel, "fixture.from_commit.paths")
        if problem:
            problems.append(("unsafe_path", problem))
    for rel in obj.get("harness_paths", []):
        problem = _path_problem(rel, "harness_paths")
        if problem:
            problems.append(("unsafe_path", problem))
    if obj.get("allowed_bash") and "Bash" not in obj.get("allowed_tools", []):
        problems.append(("tools", "allowed_bash is set but Bash is not in allowed_tools"))

    seen: set[str] = set()
    for g in graders:
        gid = g["id"]
        if gid in seen:
            problems.append(("grader", f"duplicate grader id {gid!r}"))
        seen.add(gid)
        kind = g["type"]
        if kind == "answer_matches":
            try:
                re.compile(g["pattern"], re.S)
            except re.error as exc:
                problems.append(("grader", f"grader {gid}: invalid pattern: {exc}"))
        if "path" in g:
            problem = _literal_path_problem(g["path"], f"grader {gid} path")
            if problem:
                problems.append(("unsafe_path", problem))
            elif kind == "file_unchanged" and from_commit is None and g["path"] not in files:
                problems.append(("grader", f"grader {gid}: {g['path']!r} is not a fixture file"))
        if kind == "command" and g.get("min_tests") is not None and g.get("test_format") is None:
            problems.append(("grader", f"grader {gid}: min_tests needs test_format to count tests"))
    return problems


def validate_case(obj, *, path: str = "", stem: str | None = None) -> tuple[Case | None, list[dict]]:
    """Validate one parsed manifest. Returns (Case, []) or (None, error entries)."""
    errors = [_err(path, "schema", e) for e in jsonschema_lite.validate(obj, schema())]
    if errors:
        return None, errors
    if stem is not None and obj["id"] != stem:
        return None, [_err(path, "id_mismatch", f"id {obj['id']!r} does not equal file stem {stem!r}")]
    graders = [_normalize_grader(g, i, obj["critical"]) for i, g in enumerate(obj["graders"])]
    problems = _semantic_problems(obj, graders)
    if problems:
        return None, [_err(path, code, detail) for code, detail in problems]
    fixture = obj["fixture"]
    judge = obj.get("judge")
    case = Case(
        id=obj["id"],
        task_family=obj["task_family"],
        kind=obj["kind"],
        critical=obj["critical"],
        description=obj["description"],
        request=obj["request"],
        fixture={
            "files": dict(fixture.get("files") or {}),
            "from_commit": {"paths": list(fixture["from_commit"]["paths"])} if fixture.get("from_commit") else None,
        },
        harness_paths=list(obj["harness_paths"]),
        allowed_tools=list(obj["allowed_tools"]),
        allowed_bash=list(obj["allowed_bash"]),
        network=obj["network"],
        trials_per_arm=obj["trials_per_arm"],
        timeout_seconds=obj["timeout_seconds"],
        graders=graders,
        judge={"rubric": judge["rubric"], "required": bool(judge.get("required", False))} if judge else None,
        path=path,
        raw=obj,
    )
    return case, []


def parse_case(data: bytes, *, path: str, stem: str | None = None) -> tuple[Case | None, list[dict]]:
    if len(data) > MAX_CASE_BYTES:
        return None, [_err(path, "too_large", f"{len(data)} bytes; at most {MAX_CASE_BYTES}")]
    try:
        obj = parse_json(data)
    except (UnicodeDecodeError, ValueError) as exc:
        return None, [_err(path, "invalid_json", str(exc))]
    case, errors = validate_case(obj, path=path, stem=stem)
    if case is not None:
        case.digest = hashlib.sha256(data).hexdigest()
    return case, errors


# Loading from a commit


def _tree_entries(repo, base_commit: str) -> list[tuple[str, str, str, str]] | None:
    spec = f"{base_commit}:{CASES_DIR}"
    kind = git_ops.run_git(repo, ["cat-file", "-t", spec], check=False)
    if kind.returncode != 0 or kind.stdout.decode().strip() != "tree":
        git_ops.rev_parse(repo, base_commit)  # a bad base is a GitError, not "no cases"
        return None
    out = git_ops.run_git(repo, ["ls-tree", "-z", spec]).stdout.decode("utf-8", "surrogateescape")
    entries = []
    for record in out.split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        mode, otype, sha = meta.split(" ")
        entries.append((mode, otype, sha, name))
    return sorted(entries, key=lambda e: e[3])


def load_cases(repo, base_commit: str) -> tuple[list[Case], list[dict]]:
    """Load every `.autotune/cases/*.json` at `base_commit`. Git failures raise
    git_ops.GitError; a missing cases directory gives ([], [])."""
    entries = _tree_entries(repo, base_commit)
    if entries is None:
        return [], []
    cases: list[Case] = []
    errors: list[dict] = []
    for mode, otype, sha, name in entries:
        if not name.endswith(".json"):
            continue
        path = f"{CASES_DIR}/{name}"
        if otype != "blob" or mode not in ("100644", "100755"):
            errors.append(_err(path, "unsafe_path", f"not a regular file (mode {mode}, {otype})"))
            continue
        data = git_ops.run_git(repo, ["cat-file", "blob", sha]).stdout
        case, errs = parse_case(data, path=path, stem=name[: -len(".json")])
        errors.extend(errs)
        if case is not None:
            cases.append(case)

    counts: dict[str, int] = {}
    for case in cases:
        counts[case.id] = counts.get(case.id, 0) + 1
    duplicated = {cid for cid, n in counts.items() if n > 1}
    for case in cases:
        if case.id in duplicated:
            errors.append(_err(case.path, "duplicate_id", f"case id {case.id!r} is defined more than once"))
    return [c for c in cases if c.id not in duplicated], errors


# Selection (preregistration)


def _loads(text, default):
    if isinstance(text, (list, dict)):
        return text
    if not text:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def changed_paths(candidate_row: dict) -> list[str]:
    """The candidate's changed files (validation_json.files) when known, else
    its allowed path globs (propose time, before any diff exists)."""
    validation = _loads(candidate_row.get("validation_json"), {})
    files = validation.get("files") if isinstance(validation, dict) else None
    if isinstance(files, list) and files:
        return [f for f in files if isinstance(f, str)]
    allowed = _loads(candidate_row.get("allowed_paths_json"), [])
    return [a for a in allowed if isinstance(a, str)]


def _overlaps(pattern: str, path: str) -> bool:
    return pattern == path or config.glob_match(pattern, path) or config.glob_match(path, pattern)


def touches(case: Case, paths: list[str]) -> bool:
    return any(_overlaps(h, p) for h in case.harness_paths for p in paths)


def pinned_ids(candidate_row: dict) -> list[str]:
    ids = _loads(candidate_row.get("required_cases_json"), [])
    return [i for i in ids if isinstance(i, str)]


def missing_pinned(cases: list[Case], candidate_row: dict) -> list[str]:
    known = {c.id for c in cases}
    return [i for i in pinned_ids(candidate_row) if i not in known]


def select_required(cases: list[Case], candidate_row: dict, *, paths: list[str] | None = None) -> list[Case]:
    """Required cases for a candidate, in a deterministic order (kind, then id).

    Pinned ids (candidate required_cases_json) are always included whatever
    their kind. Otherwise: target and generalization cases of the candidate's
    task_family; critical protected and negative_trigger cases whose
    harness_paths overlap the changed paths; and the lowest-id general case
    when no general case is already selected."""
    if candidate_row.get("change_kind") == "consolidation":
        return select_consolidation(cases, candidate_row, paths=paths)
    family = candidate_row.get("task_family")
    pinned = set(pinned_ids(candidate_row))
    touched = changed_paths(candidate_row) if paths is None else list(paths)
    chosen: dict[str, Case] = {}
    for case in sorted(cases, key=lambda c: c.id):
        take = case.id in pinned
        if case.kind in ("target", "generalization"):
            take = take or (family is not None and case.task_family == family)
        elif case.kind == "protected":
            take = take or (case.critical and touches(case, touched))
        elif case.kind == "negative_trigger":
            take = take or touches(case, touched)
        if take:
            chosen[case.id] = case
    if not any(c.kind == "general" for c in chosen.values()):
        general = sorted((c for c in cases if c.kind == "general"), key=lambda c: c.id)
        if general:
            chosen[general[0].id] = general[0]
    return sorted(chosen.values(), key=lambda c: (KIND_ORDER.index(c.kind), c.id))


def consolidation_dirs(candidate_row: dict) -> list[str]:
    """Skill directories of a consolidation candidate: validation_json
    consolidation.skills, else its allowed globs without a trailing `/**`."""
    validation = _loads(candidate_row.get("validation_json"), {})
    info = validation.get("consolidation") if isinstance(validation, dict) else None
    dirs = info.get("skills") if isinstance(info, dict) else None
    if isinstance(dirs, list) and dirs:
        return [d for d in dirs if isinstance(d, str) and d]
    out = []
    for g in _loads(candidate_row.get("allowed_paths_json"), []):
        if isinstance(g, str) and g:
            d = g[:-3] if g.endswith("/**") else g
            out.append(re.sub(r"\[(.)\]", r"\1", d))
    return out


def select_consolidation(cases: list[Case], candidate_row: dict, *, paths: list[str] | None = None) -> list[Case]:
    """Required cases for a consolidation: pinned ids plus every case whose
    harness_paths intersect either skill directory. At least one of them must
    be a negative_trigger case; otherwise nothing is selected and the
    candidate is inconclusive (no_required_cases)."""
    dirs = consolidation_dirs(candidate_row) if paths is None else [p[:-3] if p.endswith("/**") else p
                                                                    for p in paths]
    probes = [p for d in dirs for p in (d, f"{d}/**")]
    pinned = set(pinned_ids(candidate_row))
    chosen = [c for c in sorted(cases, key=lambda c: c.id) if c.id in pinned or touches(c, probes)]
    if not any(c.kind == "negative_trigger" and touches(c, probes) for c in chosen):
        return []
    return sorted(chosen, key=lambda c: (KIND_ORDER.index(c.kind), c.id))


# Evaluator identity


def code_digest(plugin_root: Path | None = None) -> str:
    """sha256 hex over "<relpath>\\0<bytes>\\0" for sorted autotune/*.py and prompts/*.md."""
    root = Path(plugin_root) if plugin_root is not None else _PLUGIN_ROOT
    files = sorted(
        [p for p in (root / "autotune").glob("*.py") if p.is_file()]
        + [p for p in (root / "prompts").glob("*.md") if p.is_file()],
        key=lambda p: p.relative_to(root).as_posix(),
    )
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(p.read_bytes() + b"\0")
    return h.hexdigest()


def evaluator_identity(repo, base_commit: str, *, plugin_root: Path | None = None) -> dict:
    proc = git_ops.run_git(repo, ["rev-parse", "--verify", "-q", f"{base_commit}:{EVALUATOR_DIR}"], check=False)
    tree = proc.stdout.decode().strip() if proc.returncode == 0 else None
    return {
        "cases_tree": tree or None,
        "autotune_version": __version__,
        "autotune_code": code_digest(plugin_root),
    }
