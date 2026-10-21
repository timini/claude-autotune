"""Evaluate job (M4 exit criteria) against real cases, replay, graders and gate,
with the scripted fake CLI as the replayed agent. No real model is called."""

from __future__ import annotations

import json
from unittest import mock

from autotune import evaluate, store
from autotune.util import canonical_json, iso, new_id, utc_now
from tests._cycle2_support import (
    MARKER, BASE_CLAUDE_MD, NO_VERIFY_CLAUDE_MD, USEFUL_CLAUDE_MD, Cycle2Case, case_obj, git, replay_responses, snapshot,
    standard_cases,
)


class EvaluateCase(Cycle2Case):
    def setUp(self) -> None:
        super().setUp()
        self.before = snapshot(self.repo)
        self.addCleanup(lambda: self.assertEqual(snapshot(self.repo), self.before))

    def evaluate(self, cid: str, **kw):
        return self.run_job("evaluate", cid, **kw)


class PairedReplayTest(EvaluateCase):
    def test_arms_share_the_fixture_and_differ_in_harness_commit(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        res, job = self.evaluate(cand["candidate_id"])
        self.assertEqual(job["state"], "succeeded", job)

        runs = self.runs(cand["candidate_id"])
        self.assertEqual(len(runs), 6)
        by_case: dict = {}
        for r in runs:
            by_case.setdefault(r["case_id"], {})[r["arm"]] = r
        self.assertEqual(sorted(by_case), ["protected-verify", "target-alpha", "target-beta"])
        for case_id, arms in by_case.items():
            self.assertEqual(arms["baseline"]["fixture_digest"], arms["candidate"]["fixture_digest"], case_id)
            self.assertEqual(arms["baseline"]["harness_commit"], self.base)
            self.assertEqual(arms["candidate"]["harness_commit"], cand["candidate_commit"])
            self.assertEqual({arms["baseline"]["display_order"], arms["candidate"]["display_order"]}, {1, 2})
            self.assertEqual({arms["baseline"]["display_label"], arms["candidate"]["display_label"]}, {"A", "B"})
        self.assertTrue(all(r["attempt"] == 1 and r["cli_build"] for r in runs))
        self.assertTrue(all(r["duration_ms"] is not None for r in runs))

    def test_project_instruction_change_reaches_the_replay(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        self.evaluate(cand["candidate_id"])
        calls = self.fake_calls()
        self.assertEqual(len(calls), 6)
        seen = set()
        for call in calls:
            argv = call["argv"]
            self.assertNotIn("--safe-mode", argv)
            self.assertNotIn("--bare", argv)
            i = argv.index("--setting-sources")
            self.assertEqual(argv[i + 1], "project")
            self.assertEqual(call["env"]["AUTOTUNE_ORIGIN"], "replay")
            seen.add(call["claude_md"])
        self.assertEqual(seen, {BASE_CLAUDE_MD, USEFUL_CLAUDE_MD})


class GateOutcomeTest(EvaluateCase):
    repo_config = {"enabled": True, "automatic_apply": True}

    def test_useful_change_meets_its_effect_and_is_accepted(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "succeeded", job)
        row = self.candidate(cid)
        self.assertEqual(row["status"], "accepted", row["gate_json"])
        gate = json.loads(row["gate_json"])
        self.assertEqual(gate["decision"], "accept")
        self.assertEqual(set(gate["checks"].values()), {"pass"})
        self.assertEqual(json.loads(row["required_cases_json"]),
                         ["target-alpha", "target-beta", "protected-verify"])
        self.assertEqual(json.loads(row["evaluator_commit"])["cases_tree"],
                         git(self.repo, "rev-parse", f"{self.base}:.autotune"))
        self.assertEqual(row["policy_digest"], evaluate.policy_digest(self.repo))
        outcomes = {(r["case_id"], r["arm"]): r["outcome"] for r in self.runs(cid)}
        self.assertEqual(outcomes[("target-alpha", "baseline")], "task_failure")
        self.assertEqual(outcomes[("target-alpha", "candidate")], "success")
        apply_jobs = self.jobs("apply")
        self.assertEqual([j["dedup_key"] for j in apply_jobs], [f"apply:{cid}"])
        # An accepted candidate keeps its worktree until application.
        self.assertEqual(row["worktree_path"], cand["worktree_path"])

    def test_removed_verification_is_rejected_by_a_protected_critical_case(self):
        cand = self.make_candidate({"CLAUDE.md": NO_VERIFY_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.evaluate(cid)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("rejected", "critical_regression"))
        gate = json.loads(row["gate_json"])
        self.assertEqual(gate["checks"]["critical"], "fail")
        outcomes = {(r["case_id"], r["arm"]): r["outcome"] for r in self.runs(cid)}
        self.assertEqual(outcomes[("protected-verify", "baseline")], "success")
        self.assertEqual(outcomes[("protected-verify", "candidate")], "constraint_violation")
        self.assertEqual(self.jobs("apply"), [])
        # The worktree is removed; the branch is kept for retention.
        self.assertIsNone(row["worktree_path"])
        self.assertFalse((self.data_dir / "worktrees" / cid).exists())
        self.assertEqual(git(self.repo, "rev-parse", f"autotune/{cid}"), cand["candidate_commit"])

    def test_model_mismatch_across_arms_is_inconclusive(self):
        self.script(*replay_responses(candidate_model="claude-fake-2"))
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.evaluate(cid)
        row = self.candidate(cid)
        self.assertEqual(row["status"], "inconclusive")
        gate = json.loads(row["gate_json"])
        self.assertIn("model_mismatch", gate["reason_codes"])
        self.assertEqual(gate["checks"]["integrity"], "fail")
        self.assertEqual(self.jobs("apply"), [])
        # Model freezing: every run after the first asks for the first run's actual model.
        first = min(self.runs(cid), key=lambda r: r["started_at"])
        calls = self.fake_calls()
        self.assertNotIn("--model", calls[0]["argv"])
        for call in calls[1:]:
            self.assertEqual(call["argv"][call["argv"].index("--model") + 1], first["model"])


class BrokenGraderTest(EvaluateCase):
    cases = standard_cases()[:2] + [case_obj(
        "protected-verify", "protected",
        [{"id": "suite", "type": "command", "argv": ["autotune-no-such-executable-xyz"], "expect_exit": 0}],
        critical=True)]

    def test_missing_grader_executable_is_inconclusive(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.evaluate(cid)
        row = self.candidate(cid)
        self.assertEqual(row["status"], "inconclusive", row["gate_json"])
        gate = json.loads(row["gate_json"])
        self.assertIn("incomplete", gate["reason_codes"])
        protected = [r for r in self.runs(cid) if r["case_id"] == "protected-verify"]
        self.assertEqual({r["outcome"] for r in protected}, {"ungradeable"})


class BudgetTest(EvaluateCase):
    repo_config = {"enabled": True, "experiment_calls_per_day": 3}

    def test_exhausted_budget_blocks_with_no_runs_and_full_suite_required(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "blocked_budget")
        self.assertEqual(job["last_error"], "budget_exhausted")
        self.assertEqual(self.runs(cid), [])
        self.assertEqual(self.fake_calls(), [])
        row = self.candidate(cid)
        self.assertEqual(row["status"], "evaluating")
        self.assertEqual(row["reason"], "budget_exhausted")
        self.assertEqual(json.loads(row["required_cases_json"]),
                         ["target-alpha", "target-beta", "protected-verify"])


class SelectionTest(EvaluateCase):
    cases = [case_obj("protected-verify", "protected", [{"id": "ran", "type": "tool_used", "tool": "Bash"}],
                      critical=True)]

    def test_no_required_target_case_is_inconclusive_without_runs(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "succeeded")
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("inconclusive", "no_required_cases"))
        self.assertEqual(json.loads(row["required_cases_json"]), ["protected-verify"])
        self.assertEqual(self.runs(cid), [])


class InvalidCaseTest(EvaluateCase):
    cases = standard_cases() + [{"id": "broken-case", "kind": "target"}]

    def test_invalid_case_file_makes_the_result_inconclusive(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.evaluate(cid)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("inconclusive", "invalid_cases"))
        self.assertEqual(self.runs(cid), [])


class ResumeTest(EvaluateCase):
    def test_complete_runs_are_reused_and_an_infrastructure_error_retried_once(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        # A crashed earlier attempt left preregistration, one complete run and
        # one infrastructure error.
        ctx = self.context()
        ids = ["target-alpha", "target-beta", "protected-verify"]
        with store.transaction(self.conn):
            self.conn.execute(
                "UPDATE candidates SET status = 'evaluating', required_cases_json = ?, evaluator_commit = ?, "
                "policy_digest = ? WHERE candidate_id = ?",
                (canonical_json(ids), canonical_json(evaluate.evaluator_identity(ctx, self.repo, self.base)),
                 evaluate.policy_digest(self.repo), cid))
        from autotune import replay

        ws = replay.materialize(self.repo, _case("target-alpha"), self.base, self.base, "seed1",
                                editable_globs=evaluate.harness_globs(self.cfg))
        digest = ws.fixture_digest
        replay.cleanup(ws)
        for arm, outcome, attempt, commit in (("baseline", "task_failure", 1, self.base),
                                              ("candidate", "infrastructure_error", 1, cand["candidate_commit"])):
            row = {
                "run_id": new_id("run"), "candidate_id": cid, "case_id": "target-alpha", "arm": arm, "trial": 1,
                "attempt": attempt, "fixture_digest": digest, "harness_commit": commit, "model": "claude-fake-1",
                "cli_build": "0.0.0-fake (Claude Code)", "environment_json": "{}", "outcome": outcome,
                "checks_json": canonical_json([{"id": "reports-failing", "type": "answer_contains",
                                                "status": "fail", "detail": "", "critical": False}]
                                              if outcome == "task_failure" else []),
                "usage_json": canonical_json({"input_tokens": 1, "output_tokens": 1}),
                "artifacts_json": "{}", "started_at": iso(utc_now()), "duration_ms": 5,
                "error_code": "server_error" if outcome == "infrastructure_error" else None,
            }
            names = list(row)
            with store.transaction(self.conn):
                self.conn.execute(f"INSERT INTO evaluation_runs ({', '.join(names)}) VALUES "
                                  f"({', '.join('?' for _ in names)})", [row[n] for n in names])

        res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "succeeded", job)
        runs = self.runs(cid)
        alpha = [(r["arm"], r["attempt"], r["outcome"]) for r in runs if r["case_id"] == "target-alpha"]
        self.assertEqual(alpha, [("baseline", 1, "task_failure"), ("candidate", 1, "infrastructure_error"),
                                 ("candidate", 2, "success")])
        # Five new runs: the stored baseline run was not sampled again.
        self.assertEqual(len(self.fake_calls()), 5)
        self.assertEqual(self.candidate(cid)["status"], "accepted")


class CancelTest(EvaluateCase):
    def test_cancel_between_runs_leaves_candidate_evaluating(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        real = evaluate._run_one
        calls = []

        def run_one(ctx, *a, **kw):
            out = real(ctx, *a, **kw)
            calls.append(1)
            if len(calls) == 2:
                ctx.cancel_event.set()
            return out

        with mock.patch.object(evaluate, "_run_one", run_one):
            res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "cancelled")
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("evaluating", "cancelled"))
        self.assertEqual(len(self.runs(cid)), 2)
        self.assertIsNone(row["gate_json"])


class LockTest(EvaluateCase):
    def test_busy_experiment_lock_defers(self):
        from autotune import locks, paths

        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        held = locks.FileLock(paths.machine_lock_path(), blocking=False)
        self.assertTrue(held.acquire())
        try:
            # flock is per open file description, so a second descriptor in
            # this process is refused just as another process would be.
            res, job = self.evaluate(cand["candidate_id"])
        finally:
            held.release()
        self.assertEqual(job["state"], "retry_wait")
        self.assertEqual(job["last_error"], "experiment_lock_busy")
        self.assertEqual(self.runs(cand["candidate_id"]), [])
        self.assertEqual(self.candidate(cand["candidate_id"])["status"], "validating")


class TimeoutGradingTest(EvaluateCase):
    """A run that times out is still checked by its file graders: the
    workspace exists, and a destructive edit before the hang must be seen."""

    cases = standard_cases()[:2] + [case_obj(
        "protected-verify", "protected",
        [{"id": "calc-kept", "type": "file_unchanged", "path": "calc.py"},
         {"id": "ran-tests", "type": "tool_used", "tool": "Bash"}],
        critical=True)]

    def evaluate_with_timeouts(self, cid):
        import dataclasses

        from autotune import replay

        real = replay.run_arm

        def run_arm(adapter, ws, case, *a, **kw):
            res = real(adapter, ws, case, *a, **kw)
            if case.id == "protected-verify":
                res = dataclasses.replace(res, ok=False, error_code="timeout", detail="timed out", answer=None)
            return res

        with mock.patch.object(replay, "run_arm", run_arm):
            return self.evaluate(cid)

    def test_file_change_before_a_timeout_is_a_critical_regression(self):
        responses = replay_responses()
        for r in responses:
            if r.get("when_file_contains", {}).get("text") == MARKER:
                r["edits"] = {"calc.py": "deleted\n"}
        self.script(*responses)
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        res, job = self.evaluate_with_timeouts(cid)
        self.assertEqual(job["state"], "succeeded", job)
        row = self.candidate(cid)
        self.assertEqual((row["status"], row["reason"]), ("rejected", "critical_regression"), row["gate_json"])
        prot = {r["arm"]: r for r in self.runs(cid) if r["case_id"] == "protected-verify"}
        self.assertEqual({r["outcome"] for r in prot.values()}, {"timeout"})
        checks = json.loads(prot["candidate"]["checks_json"])
        self.assertEqual([(c["id"], c["status"]) for c in checks], [("calc-kept", "fail")])
        self.assertEqual([(c["id"], c["status"]) for c in json.loads(prot["baseline"]["checks_json"])],
                         [("calc-kept", "pass")])

    def test_timeout_without_a_result_for_every_critical_grader_is_inconclusive(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.evaluate_with_timeouts(cid)
        row = self.candidate(cid)
        gate = json.loads(row["gate_json"])
        self.assertEqual(row["status"], "inconclusive", gate)
        self.assertEqual(gate["checks"]["critical"], "unknown")
        self.assertIn("critical_unknown", gate["reason_codes"])


class CancelAfterGradingTest(EvaluateCase):
    def test_a_fully_graded_run_is_stored_before_the_job_stops(self):
        from autotune import graders

        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        real = graders.run_graders

        def run_graders(*a, **kw):
            out = real(*a, **kw)
            kw["cancel_event"].set()
            return out

        with mock.patch.object(graders, "run_graders", run_graders):
            res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "cancelled")
        runs = self.runs(cid)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.fake_calls()), 1)
        self.assertIn(runs[0]["outcome"], ("success", "task_failure"))
        # The resumed job keeps that sample and does not run its slot again.
        res, job = self.evaluate(cid, dedup=f"evaluate:{cid}:resume")
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(len(self.runs(cid)), 6)
        self.assertEqual(len(self.fake_calls()), 6)

    def test_a_late_run_for_a_slot_another_worker_stored_is_dropped(self):
        from autotune import graders

        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        real_graders, real_insert = graders.run_graders, evaluate._insert_run

        def run_graders(*a, **kw):
            out = real_graders(*a, **kw)
            kw["cancel_event"].set()
            return out

        def insert_twice(conn, row):
            real_insert(conn, dict(row, run_id=new_id("run")))
            real_insert(conn, row)

        with mock.patch.object(graders, "run_graders", run_graders), \
                mock.patch.object(evaluate, "_insert_run", insert_twice):
            res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(len(self.runs(cid)), 1)

    def test_a_run_whose_grading_was_cut_short_is_discarded(self):
        from autotune import graders

        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]

        def run_graders(case, *a, **kw):
            kw["cancel_event"].set()
            return [{"id": g["id"], "type": g["type"], "critical": False, "status": "error",
                     "detail": "command cancelled"} for g in case.graders]

        with mock.patch.object(graders, "run_graders", run_graders):
            res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(self.runs(cid), [])


JUDGED_CASES = [dict(c, judge={"rubric": "The report states the observed test status.", "required": True})
                if c["id"] == "target-alpha" else c for c in standard_cases()]


def judge_reply(preference: str) -> dict:
    return {"role": "judge", "structured_output": {
        "preference": preference, "reasons": ["Compared the reports."], "cited": ["A.answer", "B.answer"]}}


class JudgeTest(EvaluateCase):
    cases = JUDGED_CASES

    def test_agreeing_judges_are_stored_counted_and_reused(self):
        self.script(*replay_responses(), judge_reply("tie"), judge_reply("tie"))
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        res, job = self.evaluate(cid)
        self.assertEqual(job["state"], "succeeded", job)
        row = self.candidate(cid)
        gate = json.loads(row["gate_json"])
        self.assertEqual(row["status"], "accepted", gate)
        self.assertEqual(gate["checks"]["judgment"], "pass")
        calls = self.fake_calls()
        self.assertEqual(len(calls), 8)
        self.assertEqual(sum(1 for c in calls if c["env"]["AUTOTUNE_ORIGIN"] == "judge"), 2)
        judged = [r for r in self.runs(cid) if r["judge_json"]]
        self.assertEqual([(r["case_id"], r["arm"], r["trial"]) for r in judged], [("target-alpha", "candidate", 1)])
        self.assertEqual(json.loads(judged[0]["judge_json"])["verdict"], "tie")
        # A resumed run of the experiment reads the stored verdict and calls no judge.
        from autotune import claude_cli, cases as cases_mod

        loaded, _ = cases_mod.load_cases(self.repo, self.base)
        exp = evaluate.Experiment(candidate_id=cid, cases=loaded, baseline_commit=self.base,
                                  candidate_commit=cand["candidate_commit"], fixture_commit=self.base)
        ctx = self.context()
        self.assertIsNone(evaluate.run_experiment(ctx, claude_cli.adapter_for(ctx), self.conn, exp))
        self.assertEqual(len(self.fake_calls()), 8)
        self.assertEqual(exp.judge_results["target-alpha"]["verdict"], "tie")

    def test_disagreeing_judges_make_the_result_inconclusive(self):
        self.script(*replay_responses(), judge_reply("A"), judge_reply("A"))
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        self.evaluate(cid)
        row = self.candidate(cid)
        gate = json.loads(row["gate_json"])
        self.assertEqual(row["status"], "inconclusive", gate)
        self.assertIn("judge_disagreement", gate["reason_codes"])
        self.assertEqual(gate["checks"]["judgment"], "fail")


class JudgeBudgetTest(EvaluateCase):
    cases = JUDGED_CASES
    repo_config = {"enabled": True, "experiment_calls_per_day": 6}

    def test_judge_calls_count_toward_the_budget(self):
        cand = self.make_candidate({"CLAUDE.md": USEFUL_CLAUDE_MD})
        cid = cand["candidate_id"]
        res, job = self.evaluate(cid)
        self.assertEqual((job["state"], job["last_error"]), ("blocked_budget", "budget_exhausted"))
        self.assertIn("8 experiment calls needed", job["last_error_detail"])
        self.assertEqual(self.runs(cid), [])
        self.assertEqual(self.fake_calls(), [])


class ArmOrderTest(EvaluateCase):
    def test_order_is_seeded_and_varies(self):
        orders = {evaluate.arm_order("cand_x", f"case-{i}", 1) for i in range(20)}
        self.assertEqual(orders, {("baseline", "candidate"), ("candidate", "baseline")})
        self.assertEqual(evaluate.arm_order("cand_x", "case-1", 2), evaluate.arm_order("cand_x", "case-1", 2))


def _case(case_id: str):
    from autotune import cases

    obj = next(c for c in standard_cases() if c["id"] == case_id)
    case, errors = cases.validate_case(obj, stem=case_id)
    assert not errors, errors
    return case


if __name__ == "__main__":
    import unittest

    unittest.main()
