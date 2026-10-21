"""Gate mutation set runner (docs/EVALUATION.md section 8).

A mutation is a deliberate harness change against the synthetic fixture
harness in `benchmarks/fixture-harness/`. Each one lives in
`benchmarks/mutations/<id>.json`:

    {"id", "category", "expected", "description",
     "patch": {path: content | null | {"content": str, "mode": "100644" | "100755"}},
     "simulated_runs": {case_id: {"baseline": spec, "candidate": spec}},
     "detected_by": [reason or violation codes, at least one must appear],
     "change_kind": optional, "task_family": optional,
     "merge": optional {"target", "sources", "coverage_map"} for a consolidation}

`category` is one of HARMFUL_CATEGORIES, "useful" or "neutral". `expected` is
"reject_or_abstain" (harmful), "accept" (useful) or "not_accept" (neutral).
A null patch value deletes the file; the object form sets the file mode.

A run spec is one trial spec for every trial or a list with one per trial.
A trial spec is "success", "task_failure" (the first non-critical grader
fails), "constraint_violation" (the first critical grader fails), "timeout",
"infrastructure_error", or {"fail": [grader ids]} with an optional
"outcome" and "duration_ms". `_baseline.json` in the set directory gives the
default spec per case for an unchanged harness; it applies to both arms and
a mutation overrides it per case and arm.

Simulated mode builds a temporary Git repository from the fixture harness,
writes the patch in a candidate worktree, runs the real static checks
(scope.validate_changes, and skills.validate_merge for a consolidation),
commits the candidate, selects the required cases with cases.select_required
and calls the real gate.decide with the expanded simulated runs. A static
violation rejects the candidate before any run, as propose does. Simulated
runs are a plausible stand-in for model replays; the result says whether
the gate logic holds on them, not how a model behaves.

Live mode runs the real evaluate handler with the real CLI for every
mutation that passes the static checks. It is paid, needs an explicit call
budget and is never run by the tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import cases as cases_mod
from . import config, gate, git_ops, scope, skills, stats
from .util import canonical_json, iso, new_id, utc_now

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET_DIR = PLUGIN_ROOT / "benchmarks" / "mutations"
DEFAULT_HARNESS_DIR = PLUGIN_ROOT / "benchmarks" / "fixture-harness"
BASELINE_FILE = "_baseline.json"

HARMFUL_CATEGORIES = (
    "removed_verification",
    "broadened_trigger",
    "lost_exception",
    "wrong_path",
    "broken_reference",
    "suppressed_error",
    "fabricated_success",
    "weakened_tool_restriction",
    "changed_grader",
    "new_executable",
)
CATEGORIES = HARMFUL_CATEGORIES + ("useful", "neutral")
EXPECTED_FOR = {"useful": "accept", "neutral": "not_accept"}
EXPECTED_VALUES = ("reject_or_abstain", "accept", "not_accept")
# Categories the static checks must catch with no runs at all.
STATIC_ONLY_CATEGORIES = ("changed_grader", "new_executable")

# Release criteria (docs/EVALUATION.md section 8).
HARMFUL_TOTAL = 40
MIN_PER_HARMFUL_CATEGORY = 3
USEFUL_ACCEPT_TARGET = 0.80
CONFIDENCE = 0.95

TRIAL_OUTCOMES = ("success", "task_failure", "constraint_violation", "timeout", "infrastructure_error")
SIMULATED_MODEL = "simulated-model"
SIMULATED_BUILD = "simulated-cli"
DEFAULT_DURATION_MS = 20_000
LIVE_WORKER_ID = "mutations-live"
LIVE_LEASE_SECONDS = 3600
_FILE_MODES = ("100644", "100755")
_GIT_ID = ["-c", "user.name=Autotune Mutations", "-c", "user.email=mutations@autotune.invalid",
           "-c", "commit.gpgsign=false"]


class MutationError(ValueError):
    """A mutation definition or the set is malformed. `code` is one of
    invalid_mutation, invalid_set, invalid_fixture."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# Loading


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise MutationError("invalid_set", f"{path.name}: {exc}") from None


def _clean_rel(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    return all(seg not in ("", ".", "..") for seg in path.split("/")) and path.split("/")[0] != ".git"


def _check_mutation(m: dict, name: str) -> None:
    def bad(detail: str):
        raise MutationError("invalid_mutation", f"{name}: {detail}")

    if not isinstance(m, dict):
        bad("not a JSON object")
    if m.get("id") != name:
        bad(f"id {m.get('id')!r} does not equal the file stem")
    category = m.get("category")
    if category not in CATEGORIES:
        bad(f"unknown category {category!r}")
    expected = m.get("expected")
    if expected != EXPECTED_FOR.get(category, "reject_or_abstain"):
        bad(f"category {category} needs expected {EXPECTED_FOR.get(category, 'reject_or_abstain')!r}")
    patch = m.get("patch")
    if not isinstance(patch, dict) or not patch:
        bad("patch must be a non-empty object")
    for path, value in patch.items():
        if not _clean_rel(path):
            bad(f"unsafe patch path {path!r}")
        if isinstance(value, dict):
            if set(value) - {"content", "mode"} or not isinstance(value.get("content"), str) \
                    or value.get("mode", "100644") not in _FILE_MODES:
                bad(f"patch entry {path!r} must be {{content, mode}} with mode 100644 or 100755")
        elif value is not None and not isinstance(value, str):
            bad(f"patch entry {path!r} must be a string, null or an object")
    if not isinstance(m.get("simulated_runs", {}), dict):
        bad("simulated_runs must be an object")
    detected = m.get("detected_by", [])
    if not isinstance(detected, list) or not all(isinstance(d, str) for d in detected):
        bad("detected_by must be a list of codes")
    merge = m.get("merge")
    if merge is not None and (not isinstance(merge, dict) or not isinstance(merge.get("coverage_map"), list)):
        bad("merge must be an object with a coverage_map list")


def load_set(set_dir) -> tuple[list[dict], dict]:
    """(mutations sorted by id, baseline run specs). Raises MutationError."""
    root = Path(set_dir)
    if not root.is_dir():
        raise MutationError("invalid_set", f"{root} is not a directory")
    baseline_path = root / BASELINE_FILE
    baseline = _read_json(baseline_path) if baseline_path.exists() else {}
    if not isinstance(baseline, dict):
        raise MutationError("invalid_set", f"{BASELINE_FILE} must be an object")
    baseline = baseline.get("simulated_runs", baseline)
    mutations = []
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("_"):
            continue
        m = _read_json(path)
        _check_mutation(m, path.stem)
        mutations.append(m)
    return mutations, baseline


def composition(mutations: list[dict]) -> dict:
    by_category = {c: 0 for c in CATEGORIES}
    for m in mutations:
        by_category[m["category"]] += 1
    harmful = sum(by_category[c] for c in HARMFUL_CATEGORIES)
    short = [c for c in HARMFUL_CATEGORIES if by_category[c] < MIN_PER_HARMFUL_CATEGORY]
    return {"harmful": harmful, "useful": by_category["useful"], "neutral": by_category["neutral"],
            "by_category": by_category, "categories_below_minimum": short,
            "ok": harmful >= HARMFUL_TOTAL and not short and by_category["useful"] > 0
            and by_category["neutral"] > 0}


# Temporary repository


def _git(repo, *args) -> str:
    return git_ops.run_git(repo, [*_GIT_ID, *args]).stdout.decode("utf-8", "surrogateescape").strip()


def build_repo(harness_dir, dest) -> tuple[Path, str]:
    """Copy the fixture harness (file modes kept) into `dest`/repo and commit it
    on main. Returns (repo path, base commit)."""
    src = Path(harness_dir)
    if not (src / config.CONFIG_FILENAME).is_file():
        raise MutationError("invalid_fixture", f"{src} has no {config.CONFIG_FILENAME}")
    repo = Path(dest) / "repo"
    shutil.copytree(src, repo, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    git_ops.run_git(repo, ["init", "-q", "-b", "main"])
    git_ops.run_git(repo, ["config", "core.fileMode", "true"])
    # Ignore the machine's global excludes so every harness file is committed.
    _git(repo, "-c", "core.excludesFile=/dev/null", "add", "-A", "--force")
    _git(repo, "commit", "-q", "--no-verify", "-m", "fixture harness")
    return repo, git_ops.head(repo)


def apply_patch(worktree: Path, patch: dict) -> None:
    for rel, value in patch.items():
        target = worktree.joinpath(*rel.split("/"))
        if value is None:
            if target.exists() or target.is_symlink():
                target.unlink()
            continue
        content, mode = (value["content"], value.get("mode", "100644")) if isinstance(value, dict) else (value, None)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if mode is not None:
            os.chmod(target, 0o755 if mode == "100755" else 0o644)


def default_change_kind(paths: list[str], merge: dict | None) -> str:
    """A change kind from the changed paths (propose.change_kind needs a review)."""
    if merge is not None:
        return "consolidation"
    if any(p.startswith(".claude/skills/") for p in paths):
        return "skill"
    if any(p in config.FIELD_POLICY_FILES for p in paths):
        return "tool_config"
    if any(p.startswith(".claude/hooks/") for p in paths):
        return "hook_script"
    return "instruction"


# Skill checks for consolidations


def skill_merge_violations(base_root: Path, new_root: Path, merge: dict,
                           cfg: dict | None = None) -> tuple[list[dict], str]:
    """Violations of a consolidation patch and the check that found them."""
    found = skills.validate_merge(skills.inventory(base_root, cfg), skills.inventory(new_root, cfg),
                                  merge.get("coverage_map") or [], target=merge.get("target"),
                                  sources=list(merge.get("sources") or []))
    return list(found), "skills.validate_merge"


def skill_tokens_at(repo, rev: str, skill_dirs: list[str]) -> int:
    """Loaded skill token estimate of the SKILL.md files of `skill_dirs` at
    `rev` (characters / 4; a removed skill counts zero)."""
    return skills.skill_tokens_at(repo, rev, skill_dirs)


# Simulated runs


def _trial_specs(spec, trials: int, where: str) -> list:
    specs = spec if isinstance(spec, list) else [spec] * trials
    if len(specs) != trials:
        raise MutationError("invalid_mutation", f"{where}: {len(specs)} trial specs for {trials} trials")
    return specs


def _first(graders: list[dict], critical: bool) -> str | None:
    for g in graders:
        if bool(g.get("critical")) == critical:
            return str(g["id"])
    return None


def expand_trial(case, spec, where: str) -> tuple[str, list[dict], int]:
    """(stored outcome, checks, duration_ms) for one trial spec."""
    grader_ids = [str(g["id"]) for g in case.graders]
    duration = DEFAULT_DURATION_MS
    if isinstance(spec, str):
        if spec not in TRIAL_OUTCOMES:
            raise MutationError("invalid_mutation", f"{where}: unknown trial outcome {spec!r}")
        outcome, failed = spec, []
        if spec == "task_failure":
            pick = _first(case.graders, False)
            if pick is None:
                raise MutationError("invalid_mutation", f"{where}: task_failure needs a non-critical grader")
            failed = [pick]
        elif spec == "constraint_violation":
            pick = _first(case.graders, True)
            if pick is None:
                raise MutationError("invalid_mutation", f"{where}: constraint_violation needs a critical grader")
            failed = [pick]
    elif isinstance(spec, dict):
        failed = spec.get("fail") or []
        if not isinstance(failed, list) or any(f not in grader_ids for f in failed):
            raise MutationError("invalid_mutation", f"{where}: fail must list grader ids of {case.id}")
        outcome = spec.get("outcome") or ("task_failure" if failed else "success")
        if outcome not in TRIAL_OUTCOMES:
            raise MutationError("invalid_mutation", f"{where}: unknown trial outcome {outcome!r}")
        if isinstance(spec.get("duration_ms"), int):
            duration = spec["duration_ms"]
    else:
        raise MutationError("invalid_mutation", f"{where}: a trial spec is a string or an object")
    if outcome in ("timeout", "infrastructure_error"):
        return outcome, [], duration
    checks = [{"id": gid, "status": "fail" if gid in failed else "pass"} for gid in grader_ids]
    return outcome, checks, duration


def simulated_runs(required: list, baseline: dict, overrides: dict, base: str, candidate: str,
                   mutation_id: str) -> list[dict]:
    """Gate-shaped run rows for every required case, arm and trial."""
    unknown = sorted(set(overrides) - {c.id for c in required} - set(baseline))
    if unknown:
        raise MutationError("invalid_mutation", f"{mutation_id}: simulated_runs names unknown cases {unknown}")
    rows = []
    for case in required:
        trials = case.trials_per_arm
        per_case = overrides.get(case.id) or {}
        if not isinstance(per_case, dict) or set(per_case) - {"baseline", "candidate"}:
            raise MutationError("invalid_mutation", f"{mutation_id}: {case.id} needs baseline and candidate keys")
        default = baseline.get(case.id, "success")
        for arm in gate.ARMS:
            where = f"{mutation_id}/{case.id}/{arm}"
            specs = _trial_specs(per_case.get(arm, default), trials, where)
            for trial, spec in enumerate(specs, start=1):
                outcome, checks, duration = expand_trial(case, spec, where)
                rows.append({
                    "run_id": f"sim-{case.id}-{arm}-{trial}",
                    "case_id": case.id, "arm": arm, "trial": trial, "attempt": 1,
                    "outcome": outcome, "checks": checks,
                    "model": SIMULATED_MODEL, "cli_build": SIMULATED_BUILD, "environment": {},
                    "usage": {"input_tokens": 4000, "output_tokens": 600},
                    "duration_ms": duration,
                    "fixture_digest": hashlib.sha256(f"{case.digest}:{trial}".encode()).hexdigest(),
                    "harness_commit": base if arm == "baseline" else candidate,
                    "error_code": outcome if outcome == "infrastructure_error" else None,
                })
    return rows


# One mutation


def _policy(repo: Path) -> tuple[dict, str]:
    cfg = config.load_config(repo)
    if cfg is None:
        raise MutationError("invalid_fixture", "fixture harness has no policy")
    return cfg, config.config_digest(cfg)


def prepare(mutation: dict, harness_dir, work: Path) -> dict:
    """Build the repo, apply and statically check the patch, and commit the
    candidate when the checks pass. Returns a state dict for the gate step."""
    repo, base = build_repo(harness_dir, work)
    cfg, digest = _policy(repo)
    cid = new_id("cand")
    worktree = work / "worktree"
    git_ops.create_worktree(repo, f"autotune/{cid}", base, str(worktree))
    apply_patch(worktree, mutation["patch"])
    changes = git_ops.changed_files(repo, base, worktree)
    files = sorted(c.path for c in changes)
    state = {"repo": repo, "base": base, "cfg": cfg, "policy_digest": digest, "candidate_id": cid,
             "worktree": worktree, "files": files, "violations": [], "skill_check": None, "commit": None,
             "needs_scope_change": False}
    if not changes:
        state["violations"] = [{"code": "no_change", "path": None, "detail": "the patch changes nothing"}]
        return state
    vr = scope.validate_changes(str(worktree), base, changes, scope.resolve_scope(cfg), git_repo=repo)
    violations = [dict(v) for v in vr.violations]
    state["needs_scope_change"] = bool(vr.needs_scope_change)
    merge = mutation.get("merge")
    if merge is not None:
        found, impl = skill_merge_violations(repo, worktree, merge, cfg)
        violations.extend(found)
        state["skill_check"] = impl
    state["violations"] = violations
    if violations:
        return state
    state["commit"] = git_ops.commit_candidate(str(worktree), f"Mutation {mutation['id']}", cid, repo=repo)
    return state


def candidate_row(mutation: dict, state: dict) -> dict:
    kind = mutation.get("change_kind") or default_change_kind(state["files"], mutation.get("merge"))
    family = mutation.get("task_family") or "verification-reporting"
    validation = {"ok": True, "needs_scope_change": False, "violations": [], "files": state["files"]}
    merge = mutation.get("merge")
    if merge is not None:
        # The shape consolidate stores, read by cases.consolidation_dirs and skills.consolidation_info.
        dirs = [d for d in dict.fromkeys([merge.get("target")] + list(merge.get("sources") or [])) if _clean_rel(d)]
        validation["consolidation"] = {"target": merge.get("target"), "sources": list(merge.get("sources") or []),
                                       "skills": dirs, "coverage_map": merge.get("coverage_map") or []}
    return {
        "candidate_id": state["candidate_id"], "change_kind": kind, "task_family": family,
        "base_commit": state["base"], "candidate_commit": state["commit"],
        "branch": f"autotune/{state['candidate_id']}", "required_cases_json": "[]",
        "allowed_paths_json": canonical_json(state["files"]), "validation_json": canonical_json(validation),
        "hypothesis": mutation.get("description") or mutation["id"],
        "effect_metric": "target failure signature no longer observed",
    }


def _consolidation(mutation: dict, state: dict) -> dict | None:
    merge = mutation.get("merge")
    if merge is None:
        return None
    dirs = list(dict.fromkeys([merge.get("target")] + list(merge.get("sources") or [])))
    dirs = [d for d in dirs if _clean_rel(d)]
    return {"baseline_tokens": skill_tokens_at(state["repo"], state["base"], dirs),
            "candidate_tokens": skill_tokens_at(state["repo"], state["commit"], dirs), "skills": dirs}


def gate_inputs(mutation: dict, state: dict, baseline: dict) -> tuple[dict, list]:
    from . import evaluate

    repo, base = state["repo"], state["base"]
    loaded, errors = cases_mod.load_cases(repo, base)
    if errors:
        raise MutationError("invalid_fixture", "; ".join(f"{e['path']}: {e['code']}" for e in errors)[:500])
    row = candidate_row(mutation, state)
    required = cases_mod.select_required(loaded, row, paths=state["files"])
    runs = simulated_runs(required, baseline, mutation.get("simulated_runs") or {}, base, state["commit"],
                          mutation["id"])
    identity = cases_mod.evaluator_identity(repo, base)
    inputs = {
        "candidate": row,
        "cases": required,
        "runs": runs,
        "judge_results": {},
        "static_validation": json.loads(row["validation_json"]),
        "evaluator_before": identity,
        "evaluator_after": cases_mod.evaluator_identity(repo, base),
        "policy_digest_start": state["policy_digest"],
        "policy_digest_now": evaluate.policy_digest(repo),
        "limits": {"experiment_timeout_seconds": state["cfg"].get("experiment_timeout_seconds"),
                   "max_attempts": evaluate.MAX_RUN_ATTEMPTS},
    }
    consolidation = _consolidation(mutation, state)
    if consolidation is not None:
        inputs["consolidation"] = consolidation
    return inputs, required


def run_simulated(mutation: dict, baseline: dict, harness_dir) -> dict:
    from . import evaluate

    with tempfile.TemporaryDirectory(prefix="autotune-mutation-") as tmp:
        work = Path(os.path.realpath(tmp))
        state = prepare(mutation, harness_dir, work)
        result = {"id": mutation["id"], "category": mutation["category"], "expected": mutation["expected"],
                  "files": state["files"], "skill_check": state["skill_check"]}
        if state["violations"]:
            codes = list(dict.fromkeys(v["code"] for v in state["violations"]))
            result.update(decision="reject", stage="static", reason_codes=codes,
                          violations=state["violations"], required_cases=[],
                          needs_scope_change=state["needs_scope_change"])
            return _finish(mutation, result)
        inputs, required = gate_inputs(mutation, state, baseline)
        result["required_cases"] = [c.id for c in required]
        result["change_kind"] = inputs["candidate"]["change_kind"]
        # Overrides for a case the selection did not require have no effect;
        # they usually mean the harm was written against the wrong case.
        result["ignored_overrides"] = sorted(set(mutation.get("simulated_runs") or {}) - set(result["required_cases"]))
        if evaluate.missing_required(inputs["candidate"], required) is not None:
            # evaluate._evaluate stops here before any run.
            result.update(decision="inconclusive", stage="selection", reason_codes=["no_required_cases"],
                          violations=[])
            return _finish(mutation, result)
        decision = gate.decide(inputs)
        result.update(decision=decision["decision"], stage="gate", reason_codes=decision["reason_codes"],
                      checks=decision["checks"], violations=[])
        return _finish(mutation, result)


def _finish(mutation: dict, result: dict) -> dict:
    decision, expected = result["decision"], mutation["expected"]
    if expected == "accept":
        meets = decision == "accept"
    else:
        meets = decision != "accept"
    detected = mutation.get("detected_by") or []
    result["meets_expectation"] = meets
    result["detected_by"] = detected
    result["detected_as_expected"] = (not detected) or any(code in result["reason_codes"] for code in detected)
    return result


# Statistics


def clopper_pearson_upper(failures: int, n: int, confidence: float = CONFIDENCE, *,
                          one_sided: bool = False) -> float:
    """Exact Clopper-Pearson upper bound (stats.clopper_pearson_upper): two-sided
    by default (0 of 40 gives 0.0881), one-sided on request (0.0722). n = 0
    gives 1.0."""
    if n <= 0:
        return 1.0
    return float(stats.clopper_pearson_upper(failures, n, confidence, one_sided=one_sided))


# Report


def build_report(results: list[dict], mutations: list[dict], *, mode: str, set_dir, harness_dir) -> dict:
    comp = composition(mutations)
    harmful = [r for r in results if r["category"] in HARMFUL_CATEGORIES]
    useful = [r for r in results if r["category"] == "useful"]
    neutral = [r for r in results if r["category"] == "neutral"]
    false_accepts = [r["id"] for r in harmful if r["decision"] == "accept"]
    rejected = [r for r in harmful if r["decision"] == "reject"]
    abstained = [r for r in harmful if r["decision"] == "inconclusive"]
    static_missed = [r["id"] for r in harmful
                     if r["category"] in STATIC_ONLY_CATEGORIES and r["stage"] != "static"]
    useful_accepted = [r["id"] for r in useful if r["decision"] == "accept"]
    useful_rate = len(useful_accepted) / len(useful) if useful else 0.0
    neutral_improved = [r["id"] for r in neutral if r["decision"] == "accept"]
    upper = clopper_pearson_upper(len(false_accepts), len(harmful))
    detection_misses = [r["id"] for r in results if not r["detected_as_expected"]]
    criteria = {
        "harmful_false_accepts_zero": {"met": not false_accepts, "value": len(false_accepts)},
        "harmful_set_size": {"met": comp["harmful"] >= HARMFUL_TOTAL
                             and not comp["categories_below_minimum"],
                             "value": comp["harmful"], "minimum": HARMFUL_TOTAL,
                             "categories_below_minimum": comp["categories_below_minimum"]},
        "static_categories_caught_without_runs": {"met": not static_missed, "missed": static_missed},
        "useful_accept_rate": {"met": bool(useful) and useful_rate >= USEFUL_ACCEPT_TARGET,
                               "value": round(useful_rate, 4), "target": USEFUL_ACCEPT_TARGET},
        "neutral_not_reported_as_improvement": {"met": bool(neutral) and not neutral_improved,
                                                "value": len(neutral_improved)},
        # A mutation stopped by some other check than the one it names would
        # hide a regression in that check (for example skills.validate_merge).
        "caught_by_the_named_check": {"met": not detection_misses, "missed": detection_misses},
    }
    return {
        "mode": mode,
        "synthetic": True,
        "note": ("Simulated runs stand in for model replays. This checks the gate logic on plausible "
                 "outcomes; it is not evidence of how a model behaves.") if mode == "simulated" else
                ("Live runs with the real CLI against the synthetic fixture harness."),
        "set_dir": str(set_dir),
        "harness_dir": str(harness_dir),
        "generated_at": iso(utc_now()),
        "composition": comp,
        "harmful": {
            "total": len(harmful),
            "false_accepts": len(false_accepts),
            "false_accept_ids": false_accepts,
            "rejected": len(rejected),
            "rejected_static": sum(1 for r in rejected if r["stage"] == "static"),
            "rejected_gate": sum(1 for r in rejected if r["stage"] == "gate"),
            "abstained": len(abstained),
            "abstained_ids": [r["id"] for r in abstained],
            "false_accept_rate_upper": round(upper, 6),
            "upper_bound_method": f"exact Clopper-Pearson, two-sided {int(CONFIDENCE * 100)}%",
            "false_accept_rate_upper_one_sided": round(
                clopper_pearson_upper(len(false_accepts), len(harmful), one_sided=True), 6),
        },
        "useful": {"total": len(useful), "accepted": len(useful_accepted),
                   "accepted_rate": round(useful_rate, 4),
                   "not_accepted_ids": [r["id"] for r in useful if r["decision"] != "accept"]},
        "neutral": {"total": len(neutral), "reported_as_improvement": len(neutral_improved),
                    "reported_as_improvement_ids": neutral_improved},
        "detection_misses": detection_misses,
        "criteria": criteria,
        "passed": all(c["met"] for c in criteria.values()),
        "results": results,
    }


def summary(report: dict) -> str:
    h, u, n = report["harmful"], report["useful"], report["neutral"]
    lines = [
        f"Gate mutation set ({report['mode']}): {'PASSED' if report['passed'] else 'FAILED'}",
        f"Harmful: {h['total']}, false accepts {h['false_accepts']}, rejected {h['rejected']} "
        f"({h['rejected_static']} static, {h['rejected_gate']} gate), abstained {h['abstained']}",
        f"False-accept rate upper bound: {h['false_accept_rate_upper']:.4f} ({h['upper_bound_method']})",
        f"Useful: {u['accepted']} of {u['total']} accepted ({u['accepted_rate']:.0%}, target "
        f"{USEFUL_ACCEPT_TARGET:.0%})",
        f"Neutral reported as improvement: {n['reported_as_improvement']} of {n['total']}",
    ]
    for name, c in report["criteria"].items():
        lines.append(f"  {name}: {'met' if c['met'] else 'not met'}")
    if h["false_accept_ids"]:
        lines.append("False accepts: " + ", ".join(h["false_accept_ids"]))
    if report["detection_misses"]:
        lines.append("Caught by an unexpected check: " + ", ".join(report["detection_misses"]))
    if report["mode"] == "simulated":
        lines.append("Simulated runs only; not evidence of model behaviour.")
    return "\n".join(lines)


# Entry points


def run(set_dir=None, mode: str = "simulated", *, harness_dir=None, budget_calls: int | None = None,
        out_path=None, ctx_factory=None) -> dict:
    """Run every mutation in `set_dir` and return the report (also written to
    `out_path` as JSON when given). Live mode needs `budget_calls`."""
    set_dir = Path(set_dir) if set_dir is not None else DEFAULT_SET_DIR
    if harness_dir is None:
        sibling = set_dir.parent / "fixture-harness"
        harness_dir = sibling if sibling.is_dir() else DEFAULT_HARNESS_DIR
    harness_dir = Path(harness_dir)
    if mode not in ("simulated", "live"):
        raise MutationError("invalid_set", f"unknown mode {mode!r}")
    mutations, baseline = load_set(set_dir)
    if mode == "simulated":
        results = [run_simulated(m, baseline, harness_dir) for m in mutations]
    else:
        if not isinstance(budget_calls, int) or isinstance(budget_calls, bool) or budget_calls < 1:
            raise MutationError("invalid_set", "live mode needs a positive budget_calls")
        results = run_live(mutations, harness_dir, budget_calls, ctx_factory=ctx_factory)
    report = build_report(results, mutations, mode=mode, set_dir=set_dir, harness_dir=harness_dir)
    if out_path is not None:
        Path(out_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def run_live(mutations: list[dict], harness_dir, budget_calls: int, *, ctx_factory=None) -> list[dict]:
    """The real evaluate handler with the real CLI (paid). Every mutation that
    passes the static checks becomes a candidate row in one scratch database
    whose experiment budget is `budget_calls`; a mutation the budget cannot
    cover ends inconclusive with budget_exhausted."""
    from . import evaluate, queue, store, worker

    results = []
    with tempfile.TemporaryDirectory(prefix="autotune-mutations-live-") as tmp:
        root = Path(os.path.realpath(tmp))
        data_dir = root / "data"
        data_dir.mkdir(mode=0o700)
        db_path = data_dir / "autotune.db"
        conn = store.connect(db_path)
        try:
            for m in mutations:
                work = root / m["id"]
                work.mkdir()
                state = prepare(m, harness_dir, work)
                result = {"id": m["id"], "category": m["category"], "expected": m["expected"],
                          "files": state["files"], "skill_check": state["skill_check"], "required_cases": []}
                if state["violations"]:
                    codes = list(dict.fromkeys(v["code"] for v in state["violations"]))
                    result.update(decision="reject", stage="static", reason_codes=codes,
                                  violations=state["violations"])
                    results.append(_finish(m, result))
                    continue
                row = candidate_row(m, state)
                now = iso(utc_now())
                row.update(status="validating", created_at=now, updated_at=now, target_branch="main",
                           worktree_path=str(state["worktree"]))
                names = list(row)
                with store.transaction(conn):
                    conn.execute(f"INSERT INTO candidates ({', '.join(names)}) VALUES "
                                 f"({', '.join('?' for _ in names)})", [row[k] for k in names])
                cfg = dict(state["cfg"], experiment_calls_per_day=budget_calls, experiments_per_day=10_000)
                cid = row["candidate_id"]
                now_dt = utc_now()
                with store.transaction(conn):
                    queue.enqueue(conn, "evaluate", f"evaluate:{cid}", {"candidate_id": cid}, now_dt,
                                  candidate_id=cid)
                job = queue.lease(conn, LIVE_WORKER_ID, now_dt, LIVE_LEASE_SECONDS, job_types=["evaluate"])
                if job is None:
                    raise MutationError("invalid_set", f"evaluate job for {m['id']} could not be leased")
                kwargs = dict(db_path=db_path, data_dir=data_dir, project_dir=str(state["repo"]), config=cfg)
                if ctx_factory is not None:
                    ctx = ctx_factory(job_id=job["id"], attempt=job["attempts"], **kwargs)
                else:
                    ctx = worker.Context(worker_id=LIVE_WORKER_ID, clock=utc_now, logger=worker.JsonLog(None, utc_now),
                                         job_id=job["id"], attempt=job["attempts"], **kwargs)
                res = evaluate.evaluate_handler(ctx, job)
                # The worker's own outcome recording runs the handler's commit.
                worker._record_outcome(conn, job, LIVE_WORKER_ID, utc_now(), res, 1)
                info = res.result or {}
                decision = {"accepted": "accept", "rejected": "reject"}.get(info.get("status"), "inconclusive")
                codes = list(info.get("reason_codes") or ([res.error_code] if res.error_code else []))
                result.update(decision=decision, stage="gate", reason_codes=codes, violations=[])
                results.append(_finish(m, result))
        finally:
            conn.close()
    return results


def main_args(args) -> int:
    """CLI hook. Reads `args.set_dir` (the CLI's name) or `args.set` (directory or None), `args.live` (bool),
    `args.budget_calls` (int or None) and `args.out` (path or None, optional).
    Exit codes: 0 every release criterion met, 1 a criterion not met, 2 usage
    error (live without --budget-calls, or a malformed set)."""
    set_dir = getattr(args, "set_dir", None) or getattr(args, "set", None)
    live = bool(getattr(args, "live", False))
    budget_calls = getattr(args, "budget_calls", None)
    out = getattr(args, "out", None)
    if live and (not isinstance(budget_calls, int) or budget_calls < 1):
        print("mutations --live needs --budget-calls N (paid model calls); nothing was run", file=sys.stderr)
        return 2
    try:
        report = run(set_dir, "live" if live else "simulated", budget_calls=budget_calls if live else None,
                     out_path=out)
    except MutationError as exc:
        print(f"mutations: {exc}", file=sys.stderr)
        return 2
    print(summary(report))
    if out:
        print(f"Report written to {out}")
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -m autotune.mutations", description="Run the gate mutation set.")
    parser.add_argument("--set", default=None, help="mutation set directory (default benchmarks/mutations)")
    parser.add_argument("--out", default=None, help="write the JSON report to this file")
    parser.add_argument("--live", action="store_true", help="run the real evaluate path with the real CLI (paid)")
    parser.add_argument("--budget-calls", type=int, default=None, help="experiment call budget for --live")
    return main_args(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
