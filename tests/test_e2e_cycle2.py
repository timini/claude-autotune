"""Cycle 2 end to end: review, authoring, paired replay, gate, application,
activation tracking, regression detection, reproduction and revert.

Real ingress, the real worker loop with the default handlers, the real adapter,
the fake CLI (tests/fake_claude.py) and a real temporary Git repo. The user's
checkout may change only by the fast-forward to the candidate and by the
revert commit.
"""

from __future__ import annotations

import io
import json
import os
import unittest

from autotune import apply, claude_cli, consolidate, evaluate, git_ops, ingress, judge, queue, replay, worker
from autotune.util import iso, utc_now
from tests._cycle2_support import (
    BASE_CLAUDE_MD, FAMILY, MARKER, USEFUL_CLAUDE_MD, Cycle2Case, git, replay_responses, snapshot,
)
from tests._runtime_support import envelope
from tests.test_review import make_review

SIG = "claims-tests-pass-when-failing"
SUMMARY = {
    "summary": "Added a rule to report failing tests.",
    "changed_behavior": "The agent reports a failing test as failing.",
    "potential_regressions": ["Longer final answers."],
    "files_changed": ["CLAUDE.md"],
}


class CycleTwoTest(Cycle2Case):
    repo_config = {"enabled": True, "automatic_apply": True}

    def reviewer(self, recommendation: str) -> dict:
        return {"role": "worker", "structured_output": make_review(
            "$echo_episode", recommendation=recommendation, signature=SIG, task_family=FAMILY)}

    def ingest_turn(self, turn_id: str) -> dict:
        now = iso(utc_now())
        env = envelope("turn_complete", self.repo, turn_id=turn_id, sent_at=now, completed_at=now,
                       started_at=now, tool_calls=[{
                           "tool": "Bash", "tool_use_id": f"tu-{turn_id}", "input_summary": "python -m unittest",
                           "is_error": True, "result_excerpt": "FAILED (failures=1)"}])
        out = io.StringIO()
        code = ingress.main(io.BytesIO(json.dumps(env).encode()), out, dict(os.environ))
        ack = json.loads(out.getvalue())
        self.assertEqual(code, 0, ack)
        return ack

    def run_worker(self) -> None:
        self.assertEqual(worker.run_worker(str(self.repo), idle_seconds=0.3, poll_seconds=0.05), 0)

    def job_states(self, job_type: str) -> list[tuple[str, str | None]]:
        return [(j["state"], j["last_error"]) for j in self.jobs(job_type)]

    def test_candidate_is_applied_then_reverted_after_a_reproduced_regression(self):
        start = snapshot(self.repo)
        self.script(self.reviewer("propose_candidate"),
                    {"role": "author", "structured_output": SUMMARY, "result": "done",
                     "edits": {"CLAUDE.md": USEFUL_CLAUDE_MD}},
                    *replay_responses())
        self.ingest_turn("turn-1")
        self.run_worker()

        cands = [dict(r) for r in self.conn.execute("SELECT * FROM candidates")]
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        cid = cand["candidate_id"]
        self.assertEqual(cand["status"], "applied", cand["gate_json"])
        gate = json.loads(cand["gate_json"])
        self.assertEqual(gate["decision"], "accept")
        self.assertEqual(json.loads(cand["required_cases_json"]), ["target-alpha", "target-beta", "protected-verify"])
        self.assertEqual(cand["applied_commit"], cand["candidate_commit"])
        self.assertEqual(cand["exposure"], "unknown")
        self.assertEqual(self.job_states("evaluate"), [("succeeded", None)])
        self.assertEqual(self.job_states("apply"), [("succeeded", None)])
        self.assertEqual(self.job_states("verify_active"), [("retry_wait", "exposure_unknown")])
        runs = self.runs(cid)
        self.assertEqual(len(runs), 6)
        self.assertEqual({r["model"] for r in runs}, {"claude-fake-1"})

        # Only the fast-forward changed the checkout: HEAD and CLAUDE.md.
        after_apply = snapshot(self.repo)
        self.assertEqual(after_apply["head"], cand["candidate_commit"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), start["head"])
        self.assertEqual(after_apply["status"], "")
        self.assertEqual(after_apply["branch"], "main")
        changed = {p for p in start["files"].keys() | after_apply["files"].keys()
                   if start["files"].get(p) != after_apply["files"].get(p)}
        self.assertEqual(changed, {"CLAUDE.md"})
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), USEFUL_CLAUDE_MD)

        # A later turn at the applied HEAD shows the same failure, and the
        # applied harness no longer helps in replay.
        self.script(self.reviewer("investigate"), *[r for r in replay_responses()
                                                    if MARKER not in json.dumps(r.get("when_file_contains"))])
        ack = self.ingest_turn("turn-2")
        repo_facts = json.loads(self.conn.execute(
            "SELECT repository_json FROM episodes WHERE episode_id = ?", (ack["episode_id"],)).fetchone()[0])
        self.assertEqual(repo_facts["head"], cand["candidate_commit"])
        self.run_worker()

        row = self.candidate(cid)
        self.assertEqual(row["status"], "reverted", row["reason"])
        reproduce = self.jobs("reproduce_regression")
        self.assertEqual(len(reproduce), 1)
        self.assertEqual(reproduce[0]["state"], "succeeded")
        self.assertTrue(json.loads(reproduce[0]["result_json"])["reverted_commit"])
        self.assertIsNone(self.conn.execute(
            "SELECT value FROM meta WHERE key = 'apply_paused_reason'").fetchone())
        new_runs = [r for r in self.runs(cid) if r["attempt"] == 2]
        self.assertEqual(len(new_runs), 6)
        self.assertEqual({r["harness_commit"] for r in new_runs if r["arm"] == "baseline"}, {start["head"]})

        # Only the revert commit changed the checkout since: the tree is back to the base.
        end = snapshot(self.repo)
        self.assertEqual(end["head"], row["reverted_commit"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), cand["candidate_commit"])
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "3")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^{tree}"), git(self.repo, "rev-parse", f"{start['head']}^{{tree}}"))
        self.assertEqual(end["files"], start["files"])
        self.assertEqual(end["status"], "")
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), BASE_CLAUDE_MD)
        self.assertEqual(git_ops.find_trailer_commits_for(self.repo, git_ops.REVERT_TRAILER_KEY, cid,
                                                          refs=["refs/heads/main"]), [row["reverted_commit"]])
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)


class Cycle2CoverageTest(unittest.TestCase):
    def test_every_emitted_code_is_classified(self):
        emitted = (set(claude_cli.ERROR_CODES) | set(evaluate.ERROR_CODES) | set(evaluate.STOP_STATUS)
                   | set(apply.ERROR_CODES) | set(replay.ERROR_CODES) | set(judge.ERROR_CODES)
                   | set(consolidate.ERROR_CODES))
        self.assertEqual(sorted(emitted - set(queue._CODE_CLASSES)), [])

    def test_cycle2_handlers_are_registered(self):
        self.assertIs(worker.HANDLERS["evaluate"], evaluate.evaluate_handler)
        self.assertIs(worker.HANDLERS["apply"], apply.apply_handler)
        self.assertIs(worker.HANDLERS["verify_active"], apply.verify_active_handler)
        self.assertIs(worker.HANDLERS["reproduce_regression"], apply.reproduce_regression_handler)
        self.assertIs(worker.HANDLERS["consolidate"], consolidate.consolidate_handler)
        self.assertEqual([m for m, _ in worker.HANDLER_IMPORT_ERRORS
                          if m in ("evaluate", "apply", "consolidate")], [])


if __name__ == "__main__":
    unittest.main()
