"""CLI for cycle 2: apply, revert, candidate evaluation detail, and status of
experiments, application and activation. Records are inserted directly; the
handlers that would act on the queued jobs are not run here."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from autotune import cli, paths, queue, store
from autotune.util import canonical_json, iso, new_id, utc_now

from tests._runtime_support import RuntimeCase, git


class ApplyCliCase(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        paths.project_data_dir(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.conn = self.db()
        self.base = git(self.repo, "rev-parse", "HEAD")

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--project-dir", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def status_json(self) -> dict:
        code, out, _ = self.cli("status", "--json")
        self.assertEqual(code, 0)
        return json.loads(out)

    def candidate(self, status, *, reason=None, required=(), applied_commit=None, **cols) -> str:
        cid = new_id("cand")
        now = iso(utc_now())
        row = {
            "candidate_id": cid, "hypothesis": "Report failing tests (claims-tests-pass)", "effect_metric": "m",
            "allowed_paths_json": '["CLAUDE.md"]', "change_kind": "instruction", "branch": f"autotune/{cid}",
            "base_commit": self.base, "candidate_commit": "c" * 40, "status": status, "reason": reason,
            "required_cases_json": canonical_json(list(required)), "applied_commit": applied_commit,
            "target_branch": "main", "created_at": now, "updated_at": now, **cols,
        }
        with store.transaction(self.conn):
            self.conn.execute(
                f"INSERT INTO candidates ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                list(row.values()),
            )
        return cid

    def run_row(self, cid, case_id, arm, trial, outcome, *, attempt=1, checks=(), error_code=None, judge=None):
        with store.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO evaluation_runs (run_id, candidate_id, case_id, arm, trial, attempt, harness_commit, "
                "model, outcome, checks_json, judge_json, error_code, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'claude-fake-1', ?, ?, ?, ?, ?, ?)",
                (new_id("run"), cid, case_id, arm, trial, attempt, self.base, outcome, canonical_json(list(checks)),
                 canonical_json(judge) if judge else None, error_code, iso(utc_now()),
                 iso(utc_now()) if outcome else None),
            )

    def jobs(self, job_type=None) -> list[dict]:
        sql, args = "SELECT * FROM jobs", ()
        if job_type:
            sql, args = sql + " WHERE job_type = ?", (job_type,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]


class ApplyCommandTest(ApplyCliCase):
    def test_apply_queues_one_job_for_an_accepted_candidate(self):
        cid = self.candidate("accepted")
        code, out, _ = self.cli("apply", cid)
        self.assertEqual(code, 0, out)
        [job] = self.jobs("apply")
        self.assertEqual((job["dedup_key"], job["candidate_id"], job["state"]), (f"apply:{cid}", cid, "queued"))
        self.assertEqual(json.loads(job["payload_json"]), {"candidate_id": cid, "requested_by": "user"})
        self.assertIn(f"Queued apply job {job['id']} for {cid}", out)
        self.assertIn("fast-forwards", out)
        self.assertIn(self.base[:12], out)

        code, out, _ = self.cli("apply", cid)
        self.assertEqual(code, 0)
        self.assertIn(f"Already pending: apply job {job['id']}", out)
        self.assertEqual(len(self.jobs("apply")), 1)

    def test_apply_makes_a_deferred_job_eligible_and_requeues_a_finished_one(self):
        cid = self.candidate("waiting_to_apply", reason="checkout_dirty")
        job_id, _ = queue.enqueue(self.conn, "apply", f"apply:{cid}", {"candidate_id": cid}, utc_now(),
                                  candidate_id=cid)
        future = "2999-01-01T00:00:00.000Z"
        with store.transaction(self.conn):
            self.conn.execute("UPDATE jobs SET state = 'retry_wait', next_eligible_at = ?, "
                              "last_error = 'checkout_dirty' WHERE id = ?", (future, job_id))
        self.cli("apply", cid)
        job = self.jobs("apply")[0]
        self.assertEqual(job["state"], "queued")
        self.assertLess(job["next_eligible_at"], future)

        with store.transaction(self.conn):
            self.conn.execute("UPDATE jobs SET state = 'failed', attempts = 5, last_error = 'git_failed' "
                              "WHERE id = ?", (job_id,))
        code, out, _ = self.cli("apply", cid)
        self.assertEqual(code, 0)
        self.assertIn(f"Requeued apply job {job_id}", out)
        job = self.jobs("apply")[0]
        self.assertEqual((job["state"], job["attempts"], job["last_error"]), ("queued", 0, None))

    def test_apply_refuses_candidates_that_are_not_accepted(self):
        for status in ("validating", "evaluating", "rejected", "inconclusive", "stale", "applied"):
            cid = self.candidate(status, reason="some_reason")
            code, out, _ = self.cli("apply", cid)
            self.assertEqual(code, 1, status)
            self.assertIn(f"is {status} (some_reason)", out)
        code, out, _ = self.cli("apply", "cand_missing")
        self.assertEqual(code, 1)
        self.assertIn("No candidate cand_missing", out)
        self.assertEqual(self.jobs("apply"), [])

    def test_apply_says_when_application_is_paused(self):
        cid = self.candidate("accepted")
        store.set_meta(self.conn, "paused", "1")
        store.set_meta(self.conn, "apply_paused_reason", "regression_suspected:cand_other")
        _, out, _ = self.cli("apply", cid)
        self.assertIn("Autotune is paused", out)
        self.assertIn("regression_suspected:cand_other", out)
        self.assertEqual(len(self.jobs("apply")), 1)


class RevertCommandTest(ApplyCliCase):
    def test_revert_queues_a_revert_now_job(self):
        cid = self.candidate("observed_active", applied_commit="a" * 40)
        code, out, _ = self.cli("revert", cid)
        self.assertEqual(code, 0, out)
        [job] = self.jobs("reproduce_regression")
        self.assertEqual(job["dedup_key"], f"revert:{cid}")
        self.assertEqual(job["candidate_id"], cid)
        self.assertEqual(json.loads(job["payload_json"]),
                         {"candidate_id": cid, "mode": "revert_now", "requested_by": "user"})
        self.assertIn(f"Queued revert job {job['id']} for {cid}", out)
        self.assertIn(f"autotune/revert-{cid}", out)

    def test_revert_is_separate_from_an_automatic_reproduction(self):
        cid = self.candidate("regression_suspected", reason="revert_conflict", applied_commit="a" * 40)
        auto, _ = queue.enqueue(self.conn, "reproduce_regression", f"reproduce_regression:{cid}",
                                {"candidate_id": cid}, utc_now(), candidate_id=cid)
        code, _, _ = self.cli("revert", cid)
        self.assertEqual(code, 0)
        jobs = self.jobs("reproduce_regression")
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[1]["id"], auto)

    def test_revert_refuses_candidates_that_are_not_applied(self):
        for status in ("accepted", "waiting_to_apply", "rejected", "reverted"):
            cid = self.candidate(status)
            code, out, _ = self.cli("revert", cid)
            self.assertEqual(code, 1, status)
            self.assertIn("only an applied candidate can be reverted", out)
        self.assertEqual(self.jobs("reproduce_regression"), [])


class StatusAndDetailTest(ApplyCliCase):
    def test_status_shows_experiments_waiting_applied_and_pauses(self):
        evaluating = self.candidate("evaluating", required=[{"id": "case-a", "trials_per_arm": 2},
                                                            {"id": "case-b", "trials_per_arm": 1}])
        self.run_row(evaluating, "case-a", "baseline", 1, "success")
        self.run_row(evaluating, "case-a", "candidate", 1, "infrastructure_error")
        self.run_row(evaluating, "case-a", "candidate", 1, "success", attempt=2)
        self.run_row(evaluating, "case-a", "baseline", 2, None)
        waiting = self.candidate("waiting_to_apply", reason="checkout_dirty")
        job_id, _ = queue.enqueue(self.conn, "apply", f"apply:{waiting}", {}, utc_now(), candidate_id=waiting)
        with store.transaction(self.conn):
            self.conn.execute("UPDATE jobs SET state = 'retry_wait', last_error = 'checkout_dirty', "
                              "last_error_detail = 'CLAUDE.md is modified' WHERE id = ?", (job_id,))
        applied = self.candidate("applied", applied_commit="a" * 40, applied_at=iso(utc_now()))
        suspect = self.candidate("regression_suspected", reason="revert_conflict", applied_commit="b" * 40)
        store.set_meta(self.conn, "apply_paused_reason", f"regression_suspected:{suspect}")

        data = self.status_json()
        [exp] = data["experiments"]
        self.assertEqual((exp["candidate_id"], exp["runs_done"], exp["runs_required"], exp["cases"]),
                         (evaluating, 2, 6, 2))
        [wait] = data["waiting_to_apply"]
        self.assertEqual((wait["candidate_id"], wait["reason"]), (waiting, "checkout_dirty"))
        self.assertEqual((wait["apply_job"]["job_id"], wait["apply_job"]["reason"]), (job_id, "checkout_dirty"))
        self.assertEqual([(a["candidate_id"], a["exposure"]) for a in data["applied_unobserved"]],
                         [(applied, "unknown")])
        self.assertEqual([r["candidate_id"] for r in data["regressions"]], [suspect])
        self.assertEqual(data["apply_paused_reason"], f"regression_suspected:{suspect}")

        _, out, _ = self.cli("status")
        self.assertIn(f"{evaluating}  evaluating  runs 2/6 over 2 case(s)", out)
        self.assertIn(f"{waiting}  waiting_to_apply (checkout_dirty); job {job_id} retry_wait: checkout_dirty "
                      "(CLAUDE.md is modified)", out)
        self.assertIn(f"{applied}  {'a' * 12}  {cli.APPLIED_UNOBSERVED_NOTE}", out)
        self.assertIn("revert conflicts with later commits", out)
        self.assertIn(f"Application paused: regression_suspected:{suspect}", out)

    def test_runs_required_unknown_when_trials_cannot_be_read(self):
        cid = self.candidate("evaluating", required=["case-not-committed"])
        [exp] = self.status_json()["experiments"]
        self.assertEqual((exp["candidate_id"], exp["runs_required"]), (cid, None))
        _, out, _ = self.cli("status")
        self.assertIn(f"{cid}  evaluating  runs 0/? over 1 case(s)", out)

    def test_runs_required_from_cases_committed_at_the_base(self):
        from tests.test_propose import case_json

        case = json.loads(case_json("case-c", "bugfix", ["CLAUDE.md"]))
        case["trials_per_arm"] = 3
        (self.repo / ".autotune" / "cases").mkdir(parents=True)
        (self.repo / ".autotune" / "cases" / "case-c.json").write_text(json.dumps(case))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "case")
        self.base = git(self.repo, "rev-parse", "HEAD")
        cid = self.candidate("evaluating", required=["case-c"])
        self.run_row(cid, "case-c", "baseline", 1, "success")
        [exp] = self.status_json()["experiments"]
        self.assertEqual((exp["candidate_id"], exp["runs_done"], exp["runs_required"]), (cid, 1, 6))

    def test_status_without_cycle_2_records_is_unchanged(self):
        data = self.status_json()
        self.assertEqual((data["experiments"], data["waiting_to_apply"], data["applied_unobserved"]), ([], [], []))
        _, out, _ = self.cli("status")
        self.assertNotIn("Experiments in progress", out)
        self.assertNotIn("Application paused", out)

    def test_candidate_detail_shows_runs_gate_application_and_defer_reason(self):
        gate = {"decision": "accept", "reason_codes": [], "required_run_ids": [],
                "checks": {"integrity": "pass", "static": "pass", "critical": "pass", "known_successes": "pass",
                           "target_effect": "pass", "judgment": "pass", "cost": "pass", "completeness": "pass"}}
        cid = self.candidate("waiting_to_apply", reason="foreground_active", required=["case-a"],
                             gate_json=canonical_json(gate), task_family="verification-reporting",
                             evaluator_commit=canonical_json({"cases_tree": "t" * 40}))
        self.run_row(cid, "case-a", "baseline", 1, "constraint_violation",
                     checks=[{"id": "tests-ran", "type": "command", "status": "fail", "detail": "Ran 0 tests"},
                             {"id": "says-failed", "type": "answer_contains", "status": "pass", "detail": ""}],
                     judge={"verdict": "candidate", "error_code": None, "model": "claude-fake-1",
                            "calls": [{"order": "AB", "preference": "B", "mapped": "candidate",
                                       "reasons": ["B reports the failure"]},
                                      {"order": "BA", "preference": "A", "mapped": "candidate",
                                       "reasons": ["A reports the failure"]}]})
        self.run_row(cid, "case-a", "candidate", 1, "success",
                     checks=[{"id": "tests-ran", "type": "command", "status": "pass", "detail": ""}])
        job_id, _ = queue.enqueue(self.conn, "apply", f"apply:{cid}", {}, utc_now(), candidate_id=cid)
        with store.transaction(self.conn):
            self.conn.execute("UPDATE jobs SET state = 'retry_wait', last_error = 'foreground_active', "
                              "last_error_detail = 'session s1 active' WHERE id = ?", (job_id,))

        code, out, _ = self.cli("candidate", cid)
        self.assertEqual(code, 0, out)
        self.assertIn("task family: verification-reporting", out)
        self.assertIn("target branch: main", out)
        self.assertIn("preregistered cases: case-a", out)
        self.assertIn("case-a  baseline  trial 1  attempt 1  constraint_violation  failed graders: tests-ran", out)
        self.assertIn("case-a  candidate  trial 1  attempt 1  success  model claude-fake-1", out)
        self.assertIn("judge: candidate; order AB candidate: B reports the failure; order BA candidate: "
                      "A reports the failure", out)
        self.assertIn("gate decision: accept (reason codes: none)", out)
        self.assertIn("gate checks: integrity pass, static pass", out)
        self.assertIn("application: waiting (foreground_active)", out)
        self.assertIn(f"latest apply job: job {job_id} retry_wait: foreground_active (session s1 active)", out)
        # The evaluation detail comes before the diff, which can be long.
        self.assertLess(out.index("gate decision"), out.index("diff:"))

    def test_candidate_detail_for_applied_and_reverted(self):
        applied = self.candidate("applied", applied_commit="a" * 40, applied_at="2026-01-02T00:00:00.000Z")
        _, out, _ = self.cli("candidate", applied)
        self.assertIn(f"application: applied {'a' * 40} at 2026-01-02T00:00:00.000Z", out)
        self.assertIn(f"exposure: unknown ({cli.APPLIED_UNOBSERVED_NOTE})", out)
        self.assertIn("gate decision: not decided", out)
        self.assertIn("runs: none", out)

        reverted = self.candidate("reverted", applied_commit="a" * 40, reverted_commit="r" * 40,
                                  reverted_at="2026-01-03T00:00:00.000Z", exposure="observed")
        self.cli("revert", reverted)  # refused: already reverted
        queue.enqueue(self.conn, "reproduce_regression", f"revert:{reverted}", {"mode": "revert_now"},
                      utc_now(), candidate_id=reverted)
        _, out, _ = self.cli("candidate", reverted)
        self.assertIn(f"revert: reverted by {'r' * 40} at 2026-01-03T00:00:00.000Z", out)
        self.assertIn("exposure: observed", out)
        self.assertIn("latest revert job: job", out)


if __name__ == "__main__":
    unittest.main()
