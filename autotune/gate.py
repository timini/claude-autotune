"""Promotion gate: a pure function from recorded evaluation runs to a decision.

`decide` reads the preregistered cases, the stored runs, judge verdicts,
the static validation result and identity snapshots, and returns
accept, reject or inconclusive with reason codes and eight checks
(docs/EVALUATION.md section 7). It touches no database, file or process,
so the same inputs always give the same result. A missing or unusable
result is never an implicit pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

CHECKS = ("integrity", "static", "critical", "known_successes", "target_effect", "judgment", "cost", "completeness")

# Every reason code decide() can emit, in the order they are reported.
REASON_CODES = (
    "critical_regression",
    "regression_observed",
    "target_not_met",
    "no_required_cases",
    "model_mismatch",
    "cli_build_mismatch",
    "harness_commit_mismatch",
    "fixture_mismatch",
    "undeclared_retry",
    "evaluator_changed",
    "policy_changed",
    "ancestor_customization_present",
    "identity_unknown",
    "static_validation_failed",
    "static_unknown",
    "critical_unknown",
    "stochastic_regression",
    "target_stochastic",
    "insufficient_target_cases",
    "target_metric_unsupported",
    "consolidation_metric_unknown",
    "outcomes_not_equivalent",
    "judge_disagreement",
    "judge_prefers_baseline",
    "judge_insufficient",
    "judge_missing",
    "cost_exceeded",
    "cost_unknown",
    "incomplete",
)
REJECT_CODES = ("critical_regression", "regression_observed", "target_not_met")

ARMS = ("baseline", "candidate")
SUCCESS = "success"
FAILURE_OUTCOMES = frozenset({"task_failure", "constraint_violation", "timeout"})
INCOMPLETE_OUTCOMES = frozenset({"infrastructure_error", "invalid_output", "ungradeable"})
GRADED_OUTCOMES = frozenset({"success", "task_failure", "constraint_violation"})
KNOWN_SUCCESS_KINDS = frozenset({"protected", "negative_trigger", "general"})
# Target and generalization cases are checked for a baseline success turning
# into a candidate failure too; only their completeness is left to target_effect.
EFFECT_KINDS = frozenset({"target", "generalization"})
CORRECTION_KINDS = frozenset({"instruction", "skill", "hook_script", "tool_config", "revert"})
JUDGE_VERDICTS = ("baseline", "candidate", "tie", "insufficient", "disagreement", "error")
DEFAULT_MAX_ATTEMPTS = 2
TIMEOUT_GRACE_MS = 5000


@dataclass
class GateInputs:
    candidate: dict
    cases: list
    runs: list
    judge_results: dict = field(default_factory=dict)
    static_validation: dict | None = None
    evaluator_before: dict | None = None
    evaluator_after: dict | None = None
    policy_digest_start: str | None = None
    policy_digest_now: str | None = None
    limits: dict = field(default_factory=dict)
    # Consolidation candidates: {"baseline_tokens", "candidate_tokens", "skills"}.
    consolidation: dict | None = None


# Accessors: cases may be dicts or attribute objects (cases.Case).


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _int(value, default=None):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def _check_list(checks) -> list[dict]:
    """Grader results as a list of dicts, from a list, {"graders": [...]} or {id: result}."""
    if isinstance(checks, list):
        return [c for c in checks if isinstance(c, dict)]
    if isinstance(checks, dict):
        if isinstance(checks.get("graders"), list):
            return [c for c in checks["graders"] if isinstance(c, dict)]
        out = []
        for key, value in checks.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("id", key)
                out.append(item)
        return out
    return []


def _grader_ids(case) -> list[str]:
    return [str(_get(g, "id")) for g in (_get(case, "graders") or []) if _get(g, "id") is not None]


def _is_critical(case, check: dict) -> bool:
    """A grader's criticality: the case's grader definition, then a key on the
    result, then the case default."""
    case_critical = bool(_get(case, "critical", False))
    for g in _get(case, "graders") or []:
        if str(_get(g, "id")) == str(check.get("id")) and _get(g, "critical") is not None:
            return bool(_get(g, "critical"))
    if isinstance(check.get("critical"), bool):
        return check["critical"]
    return case_critical


def _run_id(run: dict) -> str:
    rid = run.get("run_id")
    if rid:
        return str(rid)
    return f"{run.get('case_id')}:{run.get('arm')}:{run.get('trial')}:{run.get('attempt')}"


# Run normalisation


@dataclass
class _Run:
    raw: dict
    run_id: str
    case_id: str
    arm: str
    trial: int
    attempt: int
    outcome: str | None
    critical_failed: list[str]
    ungraded: list[str]

    @property
    def complete(self) -> bool:
        return self.outcome == SUCCESS or self.outcome in FAILURE_OUTCOMES

    @property
    def success(self) -> bool:
        return self.outcome == SUCCESS

    @property
    def failure(self) -> bool:
        return self.outcome in FAILURE_OUTCOMES


def _effective_outcome(case, run: dict) -> tuple[str | None, list[str], list[str]]:
    """Recompute a graded run's outcome from its checks, so a stored "success"
    with a failing check never counts as success. Returns (outcome, failed
    critical grader ids, missing or errored grader ids)."""
    stored = run.get("outcome")
    checks = _check_list(run.get("checks"))
    by_id = {str(c.get("id")): c for c in checks}
    critical_failed = [str(c.get("id")) for c in checks if c.get("status") == "fail" and _is_critical(case, c)]
    if stored not in GRADED_OUTCOMES:
        return stored, critical_failed, []
    ungraded = [str(c.get("id")) for c in checks if c.get("status") not in ("pass", "fail")]
    ungraded += [gid for gid in _grader_ids(case) if gid not in by_id]
    if ungraded:
        return "ungradeable", critical_failed, ungraded
    if not checks and _grader_ids(case):
        return "ungradeable", critical_failed, _grader_ids(case)
    if critical_failed or stored == "constraint_violation":
        return "constraint_violation", critical_failed, []
    if any(c.get("status") == "fail" for c in checks) or stored == "task_failure":
        return "task_failure", critical_failed, []
    return SUCCESS, critical_failed, []


class _Slots:
    """Runs grouped by (case, arm, trial) with the attempt that counts."""

    def __init__(self, cases: dict, runs: list, max_attempts: int) -> None:
        self.effective: dict[tuple, _Run] = {}
        self.undeclared: list[str] = []
        groups: dict[tuple, list[dict]] = {}
        for run in runs:
            if not isinstance(run, dict) or run.get("case_id") not in cases or run.get("arm") not in ARMS:
                continue
            trial = _int(run.get("trial"))
            if trial is None:
                continue
            groups.setdefault((run["case_id"], run["arm"], trial), []).append(run)
        for key, rows in groups.items():
            rows.sort(key=lambda r: (_int(r.get("attempt"), 0), _run_id(r)))
            case = cases[key[0]]
            chosen = None
            for i, row in enumerate(rows):
                outcome, crit, ungraded = _effective_outcome(case, row)
                item = _Run(row, _run_id(row), key[0], key[1], key[2], _int(row.get("attempt"), 0),
                            outcome, crit, ungraded)
                if chosen is not None:
                    # A later attempt after a usable one, or beyond the declared
                    # retry limit, is resampling: the first usable one counts.
                    self.undeclared.append(item.run_id)
                    continue
                if i >= max_attempts:
                    self.undeclared.append(item.run_id)
                    continue
                if outcome != "infrastructure_error" or i == len(rows) - 1:
                    chosen = item
            if chosen is None:
                last = rows[min(len(rows), max_attempts) - 1]
                outcome, crit, ungraded = _effective_outcome(case, last)
                chosen = _Run(last, _run_id(last), key[0], key[1], key[2], _int(last.get("attempt"), 0),
                              outcome, crit, ungraded)
            self.effective[key] = chosen

    def get(self, case_id: str, arm: str, trial: int) -> _Run | None:
        return self.effective.get((case_id, arm, trial))


# Result assembly


class _Result:
    def __init__(self) -> None:
        self.checks = {name: "pass" for name in CHECKS}
        self.codes: list[str] = []
        self.details: list[dict] = []

    def mark(self, check: str, status: str, code: str | None = None, **detail) -> None:
        order = {"pass": 0, "unknown": 1, "fail": 2}
        if order[status] > order[self.checks[check]]:
            self.checks[check] = status
        if code is not None:
            if code not in self.codes:
                self.codes.append(code)
            self.details.append({"check": check, "status": status, "code": code, **detail})


def _trials(case) -> int:
    n = _int(_get(case, "trials_per_arm"), 1)
    return n if n and n > 0 else 1


# Checks


def _integrity(res: _Result, inp: GateInputs, cases: dict, slots: _Slots) -> None:
    base = inp.candidate.get("base_commit")
    cand = inp.candidate.get("candidate_commit")
    expected = {"baseline": base, "candidate": cand}
    models, builds = set(), set()
    for run in slots.effective.values():
        raw = run.raw
        if run.outcome == "infrastructure_error":
            continue
        model, build = raw.get("model"), raw.get("cli_build")
        if not model:
            res.mark("integrity", "fail", "model_mismatch", run_id=run.run_id, detail="run has no recorded model")
        else:
            models.add(model)
        if not build:
            res.mark("integrity", "fail", "cli_build_mismatch", run_id=run.run_id, detail="run has no recorded cli build")
        else:
            builds.add(build)
        if not expected[run.arm] or raw.get("harness_commit") != expected[run.arm]:
            res.mark("integrity", "fail", "harness_commit_mismatch", run_id=run.run_id,
                     detail=f"{run.arm} harness {raw.get('harness_commit')} expected {expected[run.arm]}")
    if len(models) > 1:
        res.mark("integrity", "fail", "model_mismatch", detail=", ".join(sorted(models)))
    if len(builds) > 1:
        res.mark("integrity", "fail", "cli_build_mismatch", detail=", ".join(sorted(builds)))
    if slots.undeclared:
        res.mark("integrity", "fail", "undeclared_retry", run_ids=sorted(slots.undeclared))

    for case_id, case in cases.items():
        for trial in range(1, _trials(case) + 1):
            digests = set()
            for arm in ARMS:
                run = slots.get(case_id, arm, trial)
                if run is not None and run.outcome != "infrastructure_error":
                    digests.add(run.raw.get("fixture_digest"))
            if None in digests or len(digests) > 1:
                res.mark("integrity", "fail", "fixture_mismatch", case_id=case_id, trial=trial)

    for run in slots.effective.values():
        env = run.raw.get("environment")
        env = env if isinstance(env, dict) else {}
        if env.get("ancestor_customizations") or env.get("ancestor_customization_present"):
            res.mark("integrity", "fail", "ancestor_customization_present", run_id=run.run_id)

    before, after = inp.evaluator_before, inp.evaluator_after
    if not before or not after:
        res.mark("integrity", "unknown", "identity_unknown", detail="evaluator identity not recorded")
    elif before != after:
        res.mark("integrity", "fail", "evaluator_changed")
    start, now = inp.policy_digest_start, inp.policy_digest_now
    if not start or not now:
        res.mark("integrity", "unknown", "identity_unknown", detail="policy digest not recorded")
    elif start != now:
        res.mark("integrity", "fail", "policy_changed")


def _static(res: _Result, inp: GateInputs) -> None:
    sv = inp.static_validation
    ok = sv.get("ok") if isinstance(sv, dict) else None
    if ok is True:
        return
    if ok is False:
        res.mark("static", "fail", "static_validation_failed",
                 violations=len(sv.get("violations") or []) if isinstance(sv.get("violations"), list) else None)
    else:
        res.mark("static", "unknown", "static_unknown")


def _completeness(res: _Result, cases: dict, slots: _Slots) -> None:
    for case_id, case in cases.items():
        for trial in range(1, _trials(case) + 1):
            for arm in ARMS:
                run = slots.get(case_id, arm, trial)
                if run is None:
                    res.mark("completeness", "fail", "incomplete", case_id=case_id, arm=arm, trial=trial,
                             detail="missing run")
                elif not run.complete:
                    res.mark("completeness", "fail", "incomplete", case_id=case_id, arm=arm, trial=trial,
                             run_id=run.run_id, detail=run.outcome or "no outcome",
                             ungraded=run.ungraded or None)


def _critical_ids(case) -> list[str]:
    """Ids of the case's critical graders (grader setting, else the case default)."""
    case_critical = bool(_get(case, "critical", False))
    out = []
    for g in _get(case, "graders") or []:
        gid = _get(g, "id")
        flag = _get(g, "critical")
        if gid is not None and (bool(flag) if flag is not None else case_critical):
            out.append(str(gid))
    return out


def _unevaluated_critical(case, run: _Run) -> list[str]:
    """Critical graders with no pass or fail result on this run. A timeout row
    may carry no checks at all; it is then no evidence for the critical check."""
    evaluated = {str(c.get("id")) for c in _check_list(run.raw.get("checks")) if c.get("status") in ("pass", "fail")}
    return [gid for gid in _critical_ids(case) if gid not in evaluated]


def _critical(res: _Result, cases: dict, slots: _Slots) -> None:
    completed = 0
    for case_id, case in cases.items():
        for trial in range(1, _trials(case) + 1):
            run = slots.get(case_id, "candidate", trial)
            if run is None:
                continue
            if run.critical_failed or run.outcome == "constraint_violation":
                res.mark("critical", "fail", "critical_regression", case_id=case_id, trial=trial,
                         run_id=run.run_id, graders=run.critical_failed or None)
                if run.complete:
                    completed += 1
                continue
            if run.complete:
                missing = _unevaluated_critical(case, run)
                if missing:
                    res.mark("critical", "unknown", "critical_unknown", case_id=case_id, trial=trial,
                             run_id=run.run_id, graders=missing)
                else:
                    completed += 1
            else:
                res.mark("critical", "unknown", "incomplete", case_id=case_id, trial=trial)
    if completed == 0 and res.checks["critical"] != "fail":
        res.mark("critical", "unknown", "critical_unknown", detail="no completed candidate trial")


def _arm_runs(slots: _Slots, case_id: str, case, arm: str) -> list[_Run | None]:
    return [slots.get(case_id, arm, t) for t in range(1, _trials(case) + 1)]


def _all_complete(runs: list) -> bool:
    return all(r is not None and r.complete for r in runs)


def _known_successes(res: _Result, cases: dict, slots: _Slots) -> None:
    for case_id, case in cases.items():
        kind = _get(case, "kind")
        if kind not in KNOWN_SUCCESS_KINDS and kind not in EFFECT_KINDS:
            continue
        base = _arm_runs(slots, case_id, case, "baseline")
        cand = _arm_runs(slots, case_id, case, "candidate")
        regressed = [t + 1 for t, (b, c) in enumerate(zip(base, cand))
                     if b is not None and c is not None and b.success and c.failure]
        complete = _all_complete(base) and _all_complete(cand)
        if regressed:
            reproducible = complete and all(b.success for b in base) and all(c.failure for c in cand)
            code = "regression_observed" if reproducible else "stochastic_regression"
            res.mark("known_successes", "fail", code, case_id=case_id, trials=regressed)
        elif not complete and kind in KNOWN_SUCCESS_KINDS:
            res.mark("known_successes", "unknown", "incomplete", case_id=case_id)


def _target_effect(res: _Result, inp: GateInputs, cases: dict, slots: _Slots) -> None:
    kind = inp.candidate.get("change_kind")
    if kind == "consolidation":
        _consolidation_effect(res, inp, cases, slots)
        return
    if kind not in CORRECTION_KINDS:
        res.mark("target_effect", "unknown", "target_metric_unsupported", change_kind=kind)
        return
    targets = [cid for cid, c in cases.items() if _get(c, "kind") == "target"]
    generals = [cid for cid, c in cases.items() if _get(c, "kind") == "generalization"]
    if len(targets) < 2 and not (targets and generals):
        res.mark("target_effect", "unknown", "insufficient_target_cases",
                 target_cases=len(targets), generalization_cases=len(generals))
        return
    met, complete, mixed = {}, True, []
    for case_id in targets + generals:
        case = cases[case_id]
        base = _arm_runs(slots, case_id, case, "baseline")
        cand = _arm_runs(slots, case_id, case, "candidate")
        if not (_all_complete(base) and _all_complete(cand)):
            complete = False
            met[case_id] = False
            continue
        met[case_id] = all(c.success for c in cand) and any(b.failure for b in base)
        # Outcomes are deterministic unless the candidate arm both passed and
        # failed the same case; only then is a miss treated as noise.
        if not met[case_id] and len({c.success for c in cand}) > 1:
            mixed.append(case_id)
    n_target = sum(1 for cid in targets if met[cid])
    n_general = sum(1 for cid in generals if met[cid])
    if n_target >= 2 or (n_target >= 1 and n_general >= 1):
        return
    if not complete:
        res.mark("target_effect", "unknown", "incomplete", target_met=n_target, generalization_met=n_general)
    elif mixed:
        res.mark("target_effect", "fail", "target_stochastic", cases=sorted(set(mixed)))
    else:
        res.mark("target_effect", "fail", "target_not_met", target_met=n_target, generalization_met=n_general)


CONSOLIDATION_MAX_TOKEN_RATIO = 0.9


def _consolidation_effect(res: _Result, inp: GateInputs, cases: dict, slots: _Slots) -> None:
    """Consolidation target rule: the loaded skill token estimate of the
    involved skills at the candidate commit is at most 90% of the baseline's,
    and every required case has the same success or failure per trial in
    both arms. Needs a negative_trigger case among the required cases."""
    if not any(_get(c, "kind") == "negative_trigger" for c in cases.values()):
        res.mark("target_effect", "unknown", "no_required_cases", detail="no negative_trigger case is required")
        return
    data = inp.consolidation if isinstance(inp.consolidation, dict) else {}
    base_tokens, cand_tokens = _int(data.get("baseline_tokens")), _int(data.get("candidate_tokens"))
    if base_tokens is None or cand_tokens is None or base_tokens <= 0 or cand_tokens < 0:
        res.mark("target_effect", "unknown", "consolidation_metric_unknown",
                 detail="loaded skill token estimates not recorded")
        return
    complete = True
    differing = []
    for case_id, case in cases.items():
        base = _arm_runs(slots, case_id, case, "baseline")
        cand = _arm_runs(slots, case_id, case, "candidate")
        if not (_all_complete(base) and _all_complete(cand)):
            complete = False
            continue
        if any(b.success != c.success for b, c in zip(base, cand)):
            differing.append(case_id)
    reduced = cand_tokens <= CONSOLIDATION_MAX_TOKEN_RATIO * base_tokens
    if not reduced:
        res.mark("target_effect", "fail", "target_not_met", baseline_tokens=base_tokens,
                 candidate_tokens=cand_tokens, detail="loaded skill tokens did not drop by at least 10%")
    if differing:
        res.mark("target_effect", "fail", "outcomes_not_equivalent", cases=sorted(differing))
    elif not complete:
        res.mark("target_effect", "unknown", "incomplete")


def _verdicts(value) -> list[str | None]:
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        verdict = item if isinstance(item, str) else _get(item, "verdict")
        out.append(verdict if verdict in JUDGE_VERDICTS else None)
    return out


def _judgment(res: _Result, inp: GateInputs, cases: dict) -> None:
    results = inp.judge_results or {}
    for case_id, case in cases.items():
        spec = _get(case, "judge")
        if not spec or not _get(spec, "required", False):
            continue
        value = results.get(case_id)
        verdicts = _verdicts(value) if value not in (None, []) else []
        if not verdicts:
            res.mark("judgment", "unknown", "judge_missing", case_id=case_id)
            continue
        for v in verdicts:
            if v == "disagreement":
                res.mark("judgment", "fail", "judge_disagreement", case_id=case_id)
            elif v == "baseline":
                res.mark("judgment", "fail", "judge_prefers_baseline", case_id=case_id)
            elif v == "insufficient":
                res.mark("judgment", "unknown", "judge_insufficient", case_id=case_id)
            elif v not in ("candidate", "tie"):
                res.mark("judgment", "unknown", "judge_missing", case_id=case_id, verdict=v)


def _tokens(usage) -> int | None:
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    values = [_int(usage.get(k)) for k in keys if usage.get(k) is not None]
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def _cost(res: _Result, inp: GateInputs, cases: dict, slots: _Slots) -> None:
    limits = inp.limits or {}
    total_ms, total_tokens = 0, 0
    duration_known = tokens_known = True
    for run in slots.effective.values():
        if run.arm != "candidate":
            continue
        duration = _int(run.raw.get("duration_ms"))
        tokens = _tokens(run.raw.get("usage"))
        if duration is None:
            duration_known = False
        else:
            total_ms += duration
            timeout = _get(cases[run.case_id], "timeout_seconds")
            if isinstance(timeout, (int, float)) and duration > timeout * 1000 + TIMEOUT_GRACE_MS:
                res.mark("cost", "fail", "cost_exceeded", run_id=run.run_id, duration_ms=duration,
                         timeout_seconds=timeout)
        if tokens is None:
            tokens_known = False
        else:
            total_tokens += tokens
    max_seconds = limits.get("experiment_timeout_seconds")
    if isinstance(max_seconds, (int, float)):
        if not duration_known:
            res.mark("cost", "unknown", "cost_unknown", detail="candidate run duration not recorded")
        elif total_ms > max_seconds * 1000:
            res.mark("cost", "fail", "cost_exceeded", total_ms=total_ms, limit_seconds=max_seconds)
    max_tokens = limits.get("max_candidate_tokens")
    if isinstance(max_tokens, (int, float)):
        if not tokens_known:
            res.mark("cost", "unknown", "cost_unknown", detail="candidate token usage not recorded")
        elif total_tokens > max_tokens:
            res.mark("cost", "fail", "cost_exceeded", total_tokens=total_tokens, limit_tokens=max_tokens)


# Entry point


def _inputs(inputs) -> GateInputs:
    if isinstance(inputs, GateInputs):
        return inputs
    names = {f.name for f in fields(GateInputs)}
    return GateInputs(**{k: v for k, v in dict(inputs).items() if k in names})


def _ordered_codes(codes: list[str]) -> list[str]:
    rank = {code: i for i, code in enumerate(REASON_CODES)}
    return sorted(codes, key=lambda c: rank.get(c, len(rank)))


def decide(inputs) -> dict:
    """Gate decision for one candidate. `inputs` is a GateInputs or a dict with
    its field names. Returns {"decision", "reason_codes", "required_run_ids",
    "checks", "details"}."""
    inp = _inputs(inputs)
    inp.candidate = dict(inp.candidate or {})
    cases: dict = {}
    for case in inp.cases or []:
        cid = _get(case, "id")
        if cid is not None:
            cases[str(cid)] = case
    res = _Result()
    if not cases:
        for name in CHECKS:
            res.checks[name] = "unknown"
        return {"decision": "inconclusive", "reason_codes": ["no_required_cases"], "required_run_ids": [],
                "checks": res.checks, "details": [{"check": "completeness", "status": "unknown",
                                                    "code": "no_required_cases"}]}

    max_attempts = _int((inp.limits or {}).get("max_attempts"), DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS
    slots = _Slots(cases, list(inp.runs or []), max_attempts)
    _integrity(res, inp, cases, slots)
    _static(res, inp)
    _critical(res, cases, slots)
    _known_successes(res, cases, slots)
    _target_effect(res, inp, cases, slots)
    _judgment(res, inp, cases)
    _cost(res, inp, cases, slots)
    _completeness(res, cases, slots)

    codes = _ordered_codes(res.codes)
    if any(code in REJECT_CODES for code in codes):
        decision = "reject"
    elif any(status != "pass" for status in res.checks.values()):
        decision = "inconclusive"
    else:
        decision = "accept"
    run_ids = [
        slots.effective[key].run_id
        for key in sorted(slots.effective, key=lambda k: (k[0], k[2], ARMS.index(k[1])))
    ]
    return {"decision": decision, "reason_codes": codes, "required_run_ids": run_ids,
            "checks": dict(res.checks), "details": res.details}
