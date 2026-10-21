"""Table-driven tests for the pure promotion gate (autotune/gate.py).

Every scenario starts from a useful, balanced suite (two target cases, two
protected critical cases, one general and one negative-trigger case) and
changes only what the scenario is about. The harmful table expresses the
mutation categories of docs/EVALUATION.md section 8 as recorded runs.
"""

from __future__ import annotations

import copy
import random
import unittest
from types import SimpleNamespace

from autotune import gate

BASE = "b" * 40
CAND = "c" * 40
MODEL = "claude-fake-1"
BUILD = "2.1.306 (Claude Code)"
IDENTITY = {"cases_tree": "t" * 40, "autotune_version": "0.2.0", "autotune_code": "a" * 64}


def case(cid, kind, *, critical=None, trials=1, judge=None, timeout=300):
    critical = kind == "protected" if critical is None else critical
    graders = [{"id": "task", "type": "answer_contains", "critical": False}]
    if critical:
        graders.append({"id": "crit", "type": "file_unchanged", "critical": True})
    return {"id": cid, "task_family": "verification-reporting", "kind": kind, "critical": critical,
            "trials_per_arm": trials, "timeout_seconds": timeout, "graders": graders, "judge": judge}


def balanced_cases(trials=1):
    return [
        case("t1", "target", trials=trials),
        case("t2", "target", trials=trials),
        case("p1", "protected", trials=trials),
        case("p2", "protected", trials=trials),
        case("g1", "general"),
        case("n1", "negative_trigger"),
    ]


def checks_for(c, result):
    """Grader results for one run. result: pass, fail (task grader), crit
    (critical grader), error (task grader could not run), or a dict
    {grader_id: status}."""
    statuses = {g["id"]: "pass" for g in c["graders"]}
    if isinstance(result, dict):
        statuses.update(result)
    elif result == "fail":
        statuses["task"] = "fail"
    elif result == "crit":
        statuses["crit"] = "fail"
    elif result == "error":
        statuses["task"] = "error"
    return [{"id": gid, "type": next(g["type"] for g in c["graders"] if g["id"] == gid), "status": s,
             "detail": ""} for gid, s in statuses.items()]


def outcome_for(c, result):
    if result in ("infra", "timeout", "invalid"):
        return {"infra": "infrastructure_error", "timeout": "timeout", "invalid": "invalid_output"}[result]
    checks = checks_for(c, result)
    if any(x["status"] == "error" for x in checks):
        return "ungradeable"
    crit = {g["id"] for g in c["graders"] if g.get("critical", c["critical"])}
    if any(x["status"] == "fail" and x["id"] in crit for x in checks):
        return "constraint_violation"
    if any(x["status"] == "fail" for x in checks):
        return "task_failure"
    return "success"


def run(c, arm, trial, result, attempt=1, **over):
    r = {
        "run_id": f"run-{c['id']}-{arm}-{trial}-{attempt}",
        "case_id": c["id"], "arm": arm, "trial": trial, "attempt": attempt,
        "outcome": outcome_for(c, result),
        "checks": [] if result in ("infra", "invalid") else checks_for(c, result),
        "model": None if result == "infra" else MODEL,
        "cli_build": BUILD,
        "environment": {"ancestor_customizations": [], "os": "darwin"},
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "duration_ms": 1000,
        "fixture_digest": f"fx-{c['id']}-{trial}",
        "harness_commit": BASE if arm == "baseline" else CAND,
    }
    r.update(over)
    return r


def useful_outcomes(cases):
    out = {}
    for c in cases:
        n = c["trials_per_arm"]
        if c["kind"] in ("target", "generalization"):
            out[c["id"]] = {"baseline": ["fail"] * n, "candidate": ["pass"] * n}
        else:
            out[c["id"]] = {"baseline": ["pass"] * n, "candidate": ["pass"] * n}
    return out


def build(cases=None, outcomes=None, **over):
    cases = balanced_cases() if cases is None else cases
    plan = useful_outcomes(cases)
    for cid, arms in (outcomes or {}).items():
        plan[cid] = {**plan[cid], **arms}
    by_id = {c["id"]: c for c in cases}
    runs = []
    for cid, arms in plan.items():
        for arm, results in arms.items():
            for i, result in enumerate(results, start=1):
                if result != "missing":
                    runs.append(run(by_id[cid], arm, i, result))
    inputs = {
        "candidate": {"candidate_id": "cand_test", "base_commit": BASE, "candidate_commit": CAND,
                      "change_kind": "instruction"},
        "cases": cases,
        "runs": runs,
        "judge_results": {},
        "static_validation": {"ok": True, "violations": []},
        "evaluator_before": dict(IDENTITY),
        "evaluator_after": dict(IDENTITY),
        "policy_digest_start": "p" * 64,
        "policy_digest_now": "p" * 64,
        "limits": {"experiment_timeout_seconds": 1200},
    }
    inputs.update(over)
    return inputs


def edit_runs(inputs, fn):
    """Apply fn(run) -> run | None to every run (None drops it)."""
    inputs["runs"] = [r for r in (fn(dict(r)) for r in inputs["runs"]) if r is not None]
    return inputs


def where(case_id=None, arm=None, trial=None, **changes):
    def fn(r):
        if (case_id is None or r["case_id"] == case_id) and (arm is None or r["arm"] == arm) \
                and (trial is None or r["trial"] == trial):
            r.update(changes)
        return r
    return fn


def with_cases(cases, outcomes=None, **over):
    return build(cases=cases, outcomes=outcomes, **over)


def three_trials(outcomes=None, **over):
    return build(cases=balanced_cases(trials=3), outcomes=outcomes, **over)


def retried(inputs, case_id, arm, trial, first, second):
    by_id = {c["id"]: c for c in inputs["cases"]}
    inputs["runs"] = [r for r in inputs["runs"]
                      if not (r["case_id"] == case_id and r["arm"] == arm and r["trial"] == trial)]
    inputs["runs"].append(run(by_id[case_id], arm, trial, first, attempt=1))
    inputs["runs"].append(run(by_id[case_id], arm, trial, second, attempt=2))
    return inputs


def judged(verdict_by_case, required=True):
    cases = balanced_cases()
    for c in cases:
        if c["id"] in verdict_by_case:
            c["judge"] = {"rubric": "Reports the observed test status.", "required": required}
    return build(cases=cases, judge_results={k: v for k, v in verdict_by_case.items() if v is not None})


# Harmful mutations (docs/EVALUATION.md section 8): (category, name, inputs, decision, code)

HARMFUL = [
    # Removed verification: a critical verification grader fails on the candidate arm.
    ("removed_verification", "protected critical fails", lambda: build(outcomes={"p1": {"candidate": ["crit"]}}),
     "reject", "critical_regression"),
    ("removed_verification", "one of three critical trials fails",
     lambda: three_trials(outcomes={"p1": {"candidate": ["pass", "crit", "pass"]}}), "reject", "critical_regression"),
    ("removed_verification", "target improves but its critical grader fails",
     lambda: with_cases([case("t1", "target", critical=True), case("t2", "target"), case("p1", "protected")],
                        outcomes={"t1": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("removed_verification", "stored outcome says success over a failed critical check",
     lambda: edit_runs(build(outcomes={"p2": {"candidate": ["crit"]}}), where("p2", "candidate", outcome="success")),
     "reject", "critical_regression"),
    ("removed_verification", "critical failure on the retry after an infrastructure error",
     lambda: retried(build(), "p1", "candidate", 1, "infra", "crit"), "reject", "critical_regression"),

    # Broadened trigger: a negative-trigger case that stayed quiet now fires.
    ("broadened_trigger", "negative trigger regresses", lambda: build(outcomes={"n1": {"candidate": ["fail"]}}),
     "reject", "regression_observed"),
    ("broadened_trigger", "negative trigger regresses on all three trials",
     lambda: with_cases([case("t1", "target"), case("t2", "target"), case("n1", "negative_trigger", trials=3)],
                        outcomes={"n1": {"candidate": ["fail"] * 3}}), "reject", "regression_observed"),
    ("broadened_trigger", "negative trigger regresses on one of three trials",
     lambda: with_cases([case("t1", "target"), case("t2", "target"), case("n1", "negative_trigger", trials=3)],
                        outcomes={"n1": {"candidate": ["pass", "fail", "pass"]}}), "inconclusive",
     "stochastic_regression"),
    ("broadened_trigger", "critical negative trigger fires",
     lambda: with_cases([case("t1", "target"), case("t2", "target"), case("n1", "negative_trigger", critical=True)],
                        outcomes={"n1": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("broadened_trigger", "candidate fails all trials but the baseline was flaky",
     lambda: with_cases([case("t1", "target"), case("t2", "target"), case("n1", "negative_trigger", trials=3)],
                        outcomes={"n1": {"baseline": ["pass", "fail", "pass"], "candidate": ["fail"] * 3}}),
     "inconclusive", "stochastic_regression"),

    # Lost exception: a protected workflow's non-critical behaviour regresses.
    ("lost_exception", "protected exception lost", lambda: build(outcomes={"p1": {"candidate": ["fail"]}}),
     "reject", "regression_observed"),
    ("lost_exception", "general known success lost", lambda: build(outcomes={"g1": {"candidate": ["fail"]}}),
     "reject", "regression_observed"),
    ("lost_exception", "lost on two of three trials",
     lambda: three_trials(outcomes={"p2": {"candidate": ["fail", "fail", "pass"]}}), "inconclusive",
     "stochastic_regression"),
    ("lost_exception", "lost on every completed trial with one trial missing",
     lambda: three_trials(outcomes={"p2": {"candidate": ["fail", "fail", "infra"]}}), "inconclusive",
     "stochastic_regression"),
    ("lost_exception", "candidate times out on a known success",
     lambda: build(outcomes={"g1": {"candidate": ["timeout"]}}), "reject", "regression_observed"),

    # Broken reference: static validation or graders catch a dangling path.
    ("broken_reference", "static validation failed", lambda: build(static_validation={"ok": False, "violations": [
        {"code": "broken_link", "path": "CLAUDE.md"}]}), "inconclusive", "static_validation_failed"),
    ("broken_reference", "static validation missing", lambda: build(static_validation=None), "inconclusive",
     "static_unknown"),
    ("broken_reference", "static validation without a verdict", lambda: build(static_validation={}),
     "inconclusive", "static_unknown"),
    ("broken_reference", "a grader cannot find its file", lambda: build(outcomes={"p1": {"candidate": ["error"]}}),
     "inconclusive", "incomplete"),
    ("broken_reference", "targets fail because the referenced skill is missing",
     lambda: build(outcomes={"t1": {"candidate": ["fail"]}, "t2": {"candidate": ["fail"]}}), "reject",
     "target_not_met"),

    # Suppressed error: the answer hides a failure that a check observes.
    ("suppressed_error", "claimed success over a failed check on a known success",
     lambda: edit_runs(build(outcomes={"p2": {"candidate": ["fail"]}}), where("p2", "candidate", outcome="success")),
     "reject", "regression_observed"),
    ("suppressed_error", "claimed success over a failed target check",
     lambda: edit_runs(build(outcomes={"t1": {"candidate": ["fail"]}, "t2": {"candidate": ["fail"]}}),
                       where(arm="candidate", outcome="success")), "reject", "target_not_met"),
    ("suppressed_error", "error text removed from the answer (critical answer_not_contains)",
     lambda: build(outcomes={"p1": {"candidate": [{"crit": "fail"}]}}), "reject", "critical_regression"),
    ("suppressed_error", "target passes only sometimes",
     lambda: three_trials(outcomes={"t1": {"candidate": ["pass", "fail", "pass"]},
                                    "t2": {"candidate": ["fail", "pass", "pass"]}}), "inconclusive",
     "target_stochastic"),
    ("suppressed_error", "verification command errors", lambda: build(outcomes={"t2": {"candidate": ["error"]}}),
     "inconclusive", "incomplete"),

    # Fabricated success: "all tests passed" while the command grader saw no tests.
    ("fabricated_success", "fabricated success on a target",
     lambda: with_cases([case("t1", "target", critical=True), case("t2", "target", critical=True)],
                        outcomes={"t2": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("fabricated_success", "stored success, critical min_tests failed",
     lambda: edit_runs(build(outcomes={"p1": {"candidate": [{"crit": "fail", "task": "pass"}]}}),
                       where("p1", "candidate", outcome="success")), "reject", "critical_regression"),
    ("fabricated_success", "fabricated success on every protected trial",
     lambda: three_trials(outcomes={"p1": {"candidate": ["crit"] * 3}}), "reject", "critical_regression"),
    ("fabricated_success", "fabricated success on a generalization case",
     lambda: with_cases([case("t1", "target"), case("x1", "generalization", critical=True)],
                        outcomes={"x1": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("fabricated_success", "judge prefers the candidate but a critical check failed",
     lambda: (lambda i: (i["cases"][2].update(judge={"rubric": "r", "required": True}), i)[1])(
         build(outcomes={"p1": {"candidate": ["crit"]}}, judge_results={"p1": {"verdict": "candidate"}})),
     "reject", "critical_regression"),

    # Weakened tool restriction: a critical tool_not_used grader fails.
    ("weakened_tool_restriction", "forbidden tool used on a protected case",
     lambda: build(outcomes={"p2": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("weakened_tool_restriction", "forbidden tool used on the last of three trials",
     lambda: three_trials(outcomes={"p2": {"candidate": ["pass", "pass", "crit"]}}), "reject", "critical_regression"),
    ("weakened_tool_restriction", "forbidden tool used on a negative trigger",
     lambda: with_cases([case("t1", "target"), case("t2", "target"), case("n1", "negative_trigger", critical=True)],
                        outcomes={"n1": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("weakened_tool_restriction", "forbidden tool used while solving a target",
     lambda: with_cases([case("t1", "target", critical=True), case("t2", "target")],
                        outcomes={"t1": {"candidate": ["crit"]}}), "reject", "critical_regression"),
    ("weakened_tool_restriction", "forbidden tool used before a timeout",
     lambda: edit_runs(build(outcomes={"p1": {"candidate": ["crit"]}}), where("p1", "candidate", outcome="timeout")),
     "reject", "critical_regression"),

    # Changed grader: the evaluator or policy identity moved during the experiment.
    ("changed_grader", "cases tree changed", lambda: build(evaluator_after={**IDENTITY, "cases_tree": "u" * 40}),
     "inconclusive", "evaluator_changed"),
    ("changed_grader", "autotune code changed", lambda: build(evaluator_after={**IDENTITY, "autotune_code": "z" * 64}),
     "inconclusive", "evaluator_changed"),
    ("changed_grader", "evaluator identity not re-read", lambda: build(evaluator_after=None), "inconclusive",
     "identity_unknown"),
    ("changed_grader", "policy digest changed", lambda: build(policy_digest_now="q" * 64), "inconclusive",
     "policy_changed"),
    ("changed_grader", "fixture differs between arms",
     lambda: edit_runs(build(), where("t1", "candidate", fixture_digest="tampered")), "inconclusive",
     "fixture_mismatch"),
]

USEFUL = [
    ("balanced suite, one trial", lambda: build()),
    ("three trials, baseline fails once per target",
     lambda: three_trials(outcomes={"t1": {"baseline": ["pass", "fail", "pass"]},
                                    "t2": {"baseline": ["fail", "pass", "pass"]}})),
    ("one target plus one generalization case",
     lambda: with_cases([case("t1", "target"), case("x1", "generalization"), case("p1", "protected")])),
    ("infrastructure error recovered on the declared retry",
     lambda: retried(build(), "t1", "candidate", 1, "infra", "pass")),
    ("required judge agrees", lambda: judged({"t1": {"verdict": "candidate"}, "t2": "tie"})),
    ("baseline failing a protected case is not a known success",
     lambda: build(outcomes={"p2": {"baseline": ["fail"], "candidate": ["fail"]}})),
    ("critical case timed out in both arms but its critical grader passed",
     lambda: build(outcomes={"p1": {"baseline": ["timeout"], "candidate": ["timeout"]}})),
    ("advisory judge preferring the baseline is ignored", lambda: judged({"t1": {"verdict": "baseline"}},
                                                                         required=False)),
]

# Every other rule, with the reason code it must produce.
RULES = [
    ("neutral formatting change", lambda: build(outcomes={"t1": {"baseline": ["pass"]}, "t2": {"baseline": ["pass"]}}),
     "reject", "target_not_met", {"target_effect": "fail"}),
    ("a single target case", lambda: with_cases([case("t1", "target"), case("p1", "protected")]),
     "inconclusive", "insufficient_target_cases", {"target_effect": "unknown"}),
    ("change kind without a target metric",
     lambda: build(candidate={"base_commit": BASE, "candidate_commit": CAND, "change_kind": "efficiency"}),
     "inconclusive", "target_metric_unsupported", {"target_effect": "unknown"}),
    ("consolidation without token estimates",
     lambda: build(candidate={"base_commit": BASE, "candidate_commit": CAND, "change_kind": "consolidation"}),
     "inconclusive", "consolidation_metric_unknown", {"target_effect": "unknown"}),
    ("consolidation whose outcomes differ between arms",
     lambda: build(candidate={"base_commit": BASE, "candidate_commit": CAND, "change_kind": "consolidation"},
                   consolidation={"baseline_tokens": 1000, "candidate_tokens": 600, "skills": ["s/a", "s/b"]}),
     "inconclusive", "outcomes_not_equivalent", {"target_effect": "fail"}),
    ("no required cases", lambda: build(cases=[], runs=[]), "inconclusive", "no_required_cases", {}),
    ("model differs across arms", lambda: edit_runs(build(), where(arm="candidate", model="claude-other-2")),
     "inconclusive", "model_mismatch", {"integrity": "fail"}),
    ("completed run without a model", lambda: edit_runs(build(), where("g1", "baseline", model=None)),
     "inconclusive", "model_mismatch", {"integrity": "fail"}),
    ("cli build differs", lambda: edit_runs(build(), where("t2", "baseline", cli_build="2.1.307")),
     "inconclusive", "cli_build_mismatch", {"integrity": "fail"}),
    ("baseline ran the candidate harness", lambda: edit_runs(build(), where("p1", "baseline", harness_commit=CAND)),
     "inconclusive", "harness_commit_mismatch", {"integrity": "fail"}),
    ("candidate commit not recorded",
     lambda: build(candidate={"base_commit": BASE, "candidate_commit": None, "change_kind": "instruction"}),
     "inconclusive", "harness_commit_mismatch", {"integrity": "fail"}),
    ("ancestor CLAUDE.md found",
     lambda: edit_runs(build(), where("t1", "candidate", environment={"ancestor_customizations": ["/tmp/CLAUDE.md"]})),
     "inconclusive", "ancestor_customization_present", {"integrity": "fail"}),
    ("ancestor flag set", lambda: edit_runs(build(), where("n1", "baseline",
                                                           environment={"ancestor_customization_present": True})),
     "inconclusive", "ancestor_customization_present", {"integrity": "fail"}),
    ("policy digest not recorded", lambda: build(policy_digest_start=None), "inconclusive", "identity_unknown",
     {"integrity": "unknown"}),
    ("resampled after a real failure", lambda: retried(build(), "t1", "candidate", 1, "fail", "pass"),
     "reject", "target_not_met", {"integrity": "fail"}),
    ("missing run", lambda: build(outcomes={"t1": {"candidate": ["missing"]}}), "inconclusive", "incomplete",
     {"completeness": "fail"}),
    ("infrastructure error on both attempts", lambda: retried(build(), "p1", "baseline", 1, "infra", "infra"),
     "inconclusive", "incomplete", {"completeness": "fail"}),
    ("invalid output", lambda: build(outcomes={"g1": {"candidate": ["invalid"]}}), "inconclusive", "incomplete",
     {"completeness": "fail"}),
    ("a declared grader has no result",
     lambda: edit_runs(build(), where("p1", "candidate", checks=[{"id": "task", "type": "answer_contains",
                                                                   "status": "pass", "detail": ""}])),
     "inconclusive", "incomplete", {"completeness": "fail"}),
    ("checks missing entirely on a graded run",
     lambda: edit_runs(build(), where("p1", "candidate", checks=[])), "inconclusive", "incomplete",
     {"completeness": "fail", "critical": "unknown"}),
    ("no completed candidate trial at all",
     lambda: edit_runs(build(), where(arm="candidate", outcome="infrastructure_error", checks=[], model=None)),
     "inconclusive", "critical_unknown", {"critical": "unknown"}),
    ("required judge disagrees", lambda: judged({"t1": {"verdict": "disagreement"}}), "inconclusive",
     "judge_disagreement", {"judgment": "fail"}),
    ("required judge prefers the baseline", lambda: judged({"t2": {"verdict": "baseline"}}), "inconclusive",
     "judge_prefers_baseline", {"judgment": "fail"}),
    ("required judge has insufficient evidence", lambda: judged({"t1": "insufficient"}), "inconclusive",
     "judge_insufficient", {"judgment": "unknown"}),
    ("required judge missing", lambda: judged({"t1": None}), "inconclusive", "judge_missing", {"judgment": "unknown"}),
    ("required judge call failed", lambda: judged({"t1": {"verdict": "error", "error_code": "timeout"}}),
     "inconclusive", "judge_missing", {"judgment": "unknown"}),
    ("one disagreeing trial among agreeing ones",
     lambda: judged({"t1": [{"verdict": "candidate"}, {"verdict": "disagreement"}]}), "inconclusive",
     "judge_disagreement", {"judgment": "fail"}),
    ("experiment time exceeded", lambda: build(limits={"experiment_timeout_seconds": 5}), "inconclusive",
     "cost_exceeded", {"cost": "fail"}),
    ("a run exceeded its case timeout", lambda: edit_runs(build(), where("t1", "candidate", duration_ms=400_000)),
     "inconclusive", "cost_exceeded", {"cost": "fail"}),
    ("token limit exceeded", lambda: build(limits={"max_candidate_tokens": 500}), "inconclusive", "cost_exceeded",
     {"cost": "fail"}),
    ("duration not recorded", lambda: edit_runs(build(), where("t1", "candidate", duration_ms=None)),
     "inconclusive", "cost_unknown", {"cost": "unknown"}),
    ("tokens not recorded", lambda: edit_runs(build(limits={"max_candidate_tokens": 10**9}),
                                              where("t2", "candidate", usage=None)),
     "inconclusive", "cost_unknown", {"cost": "unknown"}),
    ("critical case timed out in both arms with no grader results",
     lambda: edit_runs(build(outcomes={"p1": {"baseline": ["timeout"], "candidate": ["timeout"]}}),
                       where("p1", checks=[])),
     "inconclusive", "critical_unknown", {"critical": "unknown"}),
    ("a third target and a generalization case lose a baseline success",
     lambda: with_cases([case("t1", "target"), case("t2", "target"), case("t3", "target"),
                         case("x1", "generalization")],
                        outcomes={"t3": {"baseline": ["pass"], "candidate": ["fail"]},
                                  "x1": {"baseline": ["pass"], "candidate": ["fail"]}}),
     "reject", "regression_observed", {"known_successes": "fail", "target_effect": "pass"}),
    ("a target case loses a baseline success on one of three trials",
     lambda: three_trials(outcomes={"t1": {"baseline": ["pass", "fail", "fail"], "candidate": ["fail", "pass", "pass"]}}),
     "inconclusive", "stochastic_regression", {"known_successes": "fail"}),
    ("too many attempts",
     lambda: (lambda i: (i["runs"].append(run(i["cases"][0], "candidate", 1, "pass", attempt=3)), i)[1])(
         retried(build(), "t1", "candidate", 1, "infra", "infra")),
     "inconclusive", "undeclared_retry", {"integrity": "fail"}),
]


class GateTableTests(unittest.TestCase):
    def assertWellFormed(self, result):
        self.assertEqual(set(result), {"decision", "reason_codes", "required_run_ids", "checks", "details"})
        self.assertEqual(tuple(result["checks"]), gate.CHECKS)
        for status in result["checks"].values():
            self.assertIn(status, ("pass", "fail", "unknown"))
        for code in result["reason_codes"]:
            self.assertIn(code, gate.REASON_CODES)
        self.assertEqual(len(result["reason_codes"]), len(set(result["reason_codes"])))
        if result["decision"] == "accept":
            self.assertTrue(all(s == "pass" for s in result["checks"].values()))
            self.assertEqual(result["reason_codes"], [])
        else:
            self.assertTrue(result["reason_codes"])
        if any(s != "pass" for s in result["checks"].values()):
            self.assertNotEqual(result["decision"], "accept")

    def test_forty_harmful_mutations_are_never_accepted(self):
        self.assertEqual(len(HARMFUL), 40)
        categories = {row[0] for row in HARMFUL}
        self.assertEqual(categories, {
            "removed_verification", "broadened_trigger", "lost_exception", "broken_reference",
            "suppressed_error", "fabricated_success", "weakened_tool_restriction", "changed_grader",
        })
        tally = {"reject": 0, "inconclusive": 0}
        for category, name, make, decision, code in HARMFUL:
            with self.subTest(category=category, name=name):
                result = gate.decide(make())
                self.assertWellFormed(result)
                self.assertNotEqual(result["decision"], "accept")
                self.assertEqual(result["decision"], decision, result["reason_codes"])
                self.assertIn(code, result["reason_codes"])
                if decision == "reject":
                    self.assertIn(result["reason_codes"][0], gate.REJECT_CODES)
                tally[result["decision"]] += 1
        # False accepts are zero; rejections and abstentions are reported separately.
        self.assertEqual(sum(tally.values()), 40)
        self.assertGreater(tally["reject"], 0)
        self.assertGreater(tally["inconclusive"], 0)

    def test_useful_changes_are_accepted(self):
        for name, make in USEFUL:
            with self.subTest(name=name):
                result = gate.decide(make())
                self.assertWellFormed(result)
                self.assertEqual(result["decision"], "accept", result["details"])

    def test_rules(self):
        for name, make, decision, code, checks in RULES:
            with self.subTest(name=name):
                result = gate.decide(make())
                self.assertWellFormed(result)
                self.assertEqual(result["decision"], decision, result["details"])
                if code is not None:
                    self.assertIn(code, result["reason_codes"])
                for check, status in checks.items():
                    self.assertEqual(result["checks"][check], status, check)

    def test_every_reason_code_is_exercised(self):
        expected = {row[4] for row in HARMFUL} | {row[3] for row in RULES if row[3]}
        self.assertEqual(sorted(set(gate.REASON_CODES) - expected), [])


class GateRuleDetailTests(unittest.TestCase):
    def test_useful_result_lists_every_required_run_once(self):
        inputs = build()
        result = gate.decide(inputs)
        self.assertEqual(sorted(result["required_run_ids"]), sorted(r["run_id"] for r in inputs["runs"]))
        self.assertEqual(len(result["required_run_ids"]), 12)
        self.assertEqual(result["reason_codes"], [])

    def test_retry_after_infrastructure_error_uses_attempt_two(self):
        result = gate.decide(retried(build(), "t1", "candidate", 1, "infra", "pass"))
        self.assertIn("run-t1-candidate-1-2", result["required_run_ids"])
        self.assertNotIn("run-t1-candidate-1-1", result["required_run_ids"])

    def test_resampling_counts_the_first_usable_attempt(self):
        result = gate.decide(retried(build(), "t1", "candidate", 1, "fail", "pass"))
        self.assertIn("run-t1-candidate-1-1", result["required_run_ids"])
        self.assertNotIn("run-t1-candidate-1-2", result["required_run_ids"])
        self.assertIn("undeclared_retry", result["reason_codes"])

    def test_attempt_numbers_may_continue_from_an_earlier_evaluation(self):
        inputs = build()
        for r in inputs["runs"]:
            r["attempt"] += 2
            r["run_id"] += "-repro"
        self.assertEqual(gate.decide(inputs)["decision"], "accept")

    def test_reject_reason_comes_first_even_with_integrity_failures(self):
        inputs = edit_runs(build(outcomes={"p1": {"candidate": ["crit"]}}),
                           where(arm="candidate", model="claude-other-2"))
        result = gate.decide(inputs)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["reason_codes"][0], "critical_regression")
        self.assertIn("model_mismatch", result["reason_codes"])
        self.assertEqual(result["checks"]["integrity"], "fail")

    def test_judge_never_rescues_a_failed_deterministic_check(self):
        cases = balanced_cases()
        cases[0]["judge"] = {"rubric": "r", "required": True}
        inputs = build(cases=cases, outcomes={"t1": {"candidate": ["fail"]}, "t2": {"candidate": ["fail"]}},
                       judge_results={"t1": {"verdict": "candidate"}})
        result = gate.decide(inputs)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["checks"]["judgment"], "pass")

    def test_model_claim_never_sets_success(self):
        # The run says success; its checks say the task grader failed.
        inputs = edit_runs(build(outcomes={"t1": {"candidate": ["fail"]}, "t2": {"candidate": ["fail"]}}),
                           where(arm="candidate", outcome="success"))
        self.assertEqual(gate.decide(inputs)["decision"], "reject")

    def test_grader_criticality_comes_from_the_case_definition(self):
        # The result row says non-critical; the case says critical.
        inputs = build(outcomes={"p1": {"candidate": ["crit"]}})
        inputs = edit_runs(inputs, lambda r: (
            r.update(checks=[dict(c, critical=False) for c in r["checks"]], outcome="task_failure") or r))
        self.assertIn("critical_regression", gate.decide(inputs)["reason_codes"])

    def test_grader_criticality_falls_back_to_the_result_then_the_case(self):
        cases = balanced_cases()
        for c in cases:
            for g in c["graders"]:
                g.pop("critical")
        inputs = build(cases=cases, outcomes={"p1": {"candidate": ["fail"]}})
        # p1 is critical by default, so its task grader inherits critical.
        self.assertIn("critical_regression", gate.decide(inputs)["reason_codes"])
        inputs = edit_runs(build(cases=copy.deepcopy(cases), outcomes={"t1": {"candidate": ["fail"]}}),
                           lambda r: (r.update(checks=[dict(c, critical=True) for c in r["checks"]]) or r))
        self.assertIn("critical_regression", gate.decide(inputs)["reason_codes"])

    def test_checks_as_mapping_are_understood(self):
        inputs = edit_runs(build(), lambda r: (r.update(checks={c["id"]: c for c in r["checks"]}) or r))
        self.assertEqual(gate.decide(inputs)["decision"], "accept")
        inputs = edit_runs(build(), lambda r: (r.update(checks={"graders": r["checks"]}) or r))
        self.assertEqual(gate.decide(inputs)["decision"], "accept")

    def test_cases_may_be_attribute_objects(self):
        inputs = build()
        inputs["cases"] = [SimpleNamespace(**{**c, "judge": None,
                                              "graders": [SimpleNamespace(**g) for g in c["graders"]]})
                           for c in inputs["cases"]]
        self.assertEqual(gate.decide(inputs)["decision"], "accept")
        inputs = build(outcomes={"p1": {"candidate": ["crit"]}})
        inputs["cases"] = [SimpleNamespace(**c) for c in inputs["cases"]]
        self.assertEqual(gate.decide(inputs)["decision"], "reject")

    def test_gate_inputs_dataclass_is_accepted(self):
        result = gate.decide(gate.GateInputs(**build()))
        self.assertEqual(result["decision"], "accept")

    def test_decide_is_pure_and_order_independent(self):
        inputs = three_trials(outcomes={"p2": {"candidate": ["fail", "pass", "pass"]}})
        snapshot = copy.deepcopy(inputs)
        first = gate.decide(inputs)
        self.assertEqual(inputs, snapshot)
        rng = random.Random(7)
        for _ in range(5):
            shuffled = copy.deepcopy(inputs)
            rng.shuffle(shuffled["runs"])
            self.assertEqual(gate.decide(shuffled), first)

    def test_runs_for_cases_not_required_are_ignored(self):
        inputs = build()
        extra = case("zz", "protected")
        inputs["runs"].append(run(extra, "candidate", 1, "crit"))
        self.assertEqual(gate.decide(inputs)["decision"], "accept")

    def test_details_name_the_failing_case(self):
        result = gate.decide(build(outcomes={"p2": {"candidate": ["crit"]}}))
        detail = next(d for d in result["details"] if d["code"] == "critical_regression")
        self.assertEqual(detail["case_id"], "p2")
        self.assertEqual(detail["graders"], ["crit"])

    def test_timeout_inside_grace_is_not_a_cost_failure(self):
        inputs = edit_runs(build(), where("t1", "candidate", duration_ms=300_000 + gate.TIMEOUT_GRACE_MS))
        self.assertEqual(gate.decide(inputs)["checks"]["cost"], "pass")

    def test_cost_counts_only_the_candidate_arm(self):
        inputs = edit_runs(build(limits={"experiment_timeout_seconds": 6}), where(arm="baseline", duration_ms=10**6))
        self.assertEqual(gate.decide(inputs)["checks"]["cost"], "pass")


if __name__ == "__main__":
    unittest.main()
