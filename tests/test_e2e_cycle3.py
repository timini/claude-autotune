"""Cycle 3 end to end: skill usage capture, the local overlap check, curation,
authoring, paired replay, the consolidation gate rule and application, then
the cycle 3 commands against the same project.

Real ingress, the real worker loop with the default handlers, the real
adapter, the fake CLI (tests/fake_claude.py), the real skills module and a
real temporary Git repo with two overlapping skills: distinct exceptions, a
script support file with its executable bit, and relative links. No required
case is of kind target, so the consolidation is measured only on the cases
that touch its skills (one of them a negative_trigger case).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotune import calibrate, cases, cli, ingress, mutations, paths, skills, worker
from autotune.util import iso, utc_now
from tests._cycle2_support import Cycle2Case, case_obj, git, snapshot
from tests._runtime_support import envelope
from tests.test_consolidate import OLD_BODY_LINE, STORE_TABLES
from tests.test_review import make_review
from tests.test_skills import (
    AUDIT_DIR, MERGED, REPORTER, REPORTER_DIR, RUNNER_DIR, SKILL_FILES, full_coverage, write_tree,
)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ANSWER = "Ran the tests: 1 FAILING. Report attached."
RENAMED = "scripts/reporter-report.sh"
RENAMED_MERGED = MERGED.replace("`scripts/report.sh`", f"`{RENAMED}`")
SUMMARY = {"summary": "Merged test-reporter into test-runner.", "changed_behavior": "One skill covers both.",
           "potential_regressions": ["The merged description is longer."]}


def good_review() -> dict:
    """A passing review: nothing in it implicates the applied merge."""
    review = make_review("$echo_episode", outcome=3, recommendation="no_change", task_family="skills",
                         signature="none-observed")
    for dim in review["dimensions"].values():
        if dim.get("score") is not None:
            dim["score"] = 3
    return {"role": "worker", "structured_output": review}


def coverage() -> list[dict]:
    return [dict(e, destination=f"{RUNNER_DIR}/{RENAMED}") if e["kind"] == "support_file" else e
            for e in full_coverage()]


def skill_cases() -> list[dict]:
    reports = [{"id": "reports-failing", "type": "answer_contains", "text": "FAILING", "critical": True}]
    return [
        case_obj("runner-reports", "protected", reports, critical=True, harness=(f"{RUNNER_DIR}/**",),
                 family="skills"),
        case_obj("reporter-reports", "generalization", reports, harness=(f"{REPORTER_DIR}/**",), family="skills"),
        case_obj("skill-negative", "negative_trigger",
                 [{"id": "no-release-talk", "type": "answer_not_contains", "text": "release", "critical": True}],
                 critical=True, harness=(".claude/skills/**",), family="docs", request="Fix the typo in README."),
        case_obj("claude-md-only", "protected", reports, critical=True, harness=("CLAUDE.md",)),
    ]


class CycleThreeTest(Cycle2Case):
    repo_config = {"enabled": True, "automatic_apply": True, "executable_paths": [".claude/skills/*/scripts/*"],
                   "critical_skills": [AUDIT_DIR]}
    cases = skill_cases()

    def setUp(self) -> None:
        super().setUp()
        # Edits to the plugin tree while the suite runs must not change the
        # evaluator identity mid-experiment.
        patcher = mock.patch.object(cases, "code_digest", return_value="0" * 64)
        patcher.start()
        self.addCleanup(patcher.stop)
        write_tree(self.repo, SKILL_FILES)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "skills")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def ingest_turn(self, turn_id: str) -> dict:
        now = iso(utc_now())
        loaded = [{"skill": "test-runner", "source": "projectSettings",
                   "path": str(self.repo / RUNNER_DIR / "SKILL.md")},
                  {"skill": "test-reporter", "source": "projectSettings",
                   "path": str(self.repo / REPORTER_DIR / "SKILL.md")}]
        env = envelope("turn_complete", self.repo, turn_id=turn_id, sent_at=now, completed_at=now,
                       started_at=now, skills=loaded, tool_calls=[{
                           "tool": "Bash", "tool_use_id": f"tu-{turn_id}", "input_summary": "python -m unittest",
                           "is_error": True, "result_excerpt": "FAILED (failures=1)"}])
        out = io.StringIO()
        code = ingress.main(io.BytesIO(json.dumps(env).encode()), out, dict(os.environ))
        ack = json.loads(out.getvalue())
        self.assertEqual(code, 0, ack)
        return ack

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--project-dir", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def test_overlapping_skills_are_merged_through_the_ordinary_candidate_path(self):
        start = snapshot(self.repo)
        self.script(
            good_review(),
            {"role": "worker", "structured_output": {
                "decision": "merge", "target": RUNNER_DIR, "sources": [REPORTER_DIR], "coverage_map": coverage(),
                "rationale": "Both skills run the unit tests and report failures.",
                "risks": ["Longer description."]}},
            {"role": "author", "structured_output": SUMMARY, "result": "done",
             "edits": {f"{RUNNER_DIR}/SKILL.md": RENAMED_MERGED}},
            {"role": "replay", "result": ANSWER},
        )
        ack = self.ingest_turn("turn-1")
        episode_id = ack["episode_id"]
        self.assertEqual(worker.run_worker(str(self.repo), idle_seconds=0.3, poll_seconds=0.05), 0)

        # Review: usage rows, one per load and an outcome per skill, never inferred.
        events = [(r["skill"], r["event"]) for r in self.conn.execute(
            "SELECT skill, event FROM skill_events WHERE episode_id = ? ORDER BY id", (episode_id,))]
        self.assertEqual(sorted(events), sorted([("test-runner", "load"), ("test-reporter", "load"),
                                                 ("test-runner", "outcome"), ("test-reporter", "outcome")]))
        [review_job] = self.jobs("review")
        self.assertEqual(review_job["state"], "succeeded", review_job["last_error_detail"])

        # Local check: the pair loaded together queued one consolidate job at the base HEAD.
        [job] = self.jobs("consolidate")
        self.assertEqual(job["dedup_key"], f"consolidate:pair:{REPORTER_DIR}:{RUNNER_DIR}:{self.base}")
        self.assertEqual(json.loads(job["payload_json"])["mode"], "pair")
        self.assertEqual(job["state"], "succeeded", job["last_error_detail"])
        self.assertEqual([c["env"]["AUTOTUNE_ORIGIN"] for c in self.fake_calls()][:3],
                         ["worker", "worker", "author"])

        # The candidate went through evaluate and the consolidation gate rule.
        [cand] = [dict(r) for r in self.conn.execute("SELECT * FROM candidates")]
        cid = cand["candidate_id"]
        self.assertEqual(json.loads(job["result_json"])["candidate_id"], cid)
        self.assertEqual((cand["change_kind"], cand["status"]), ("consolidation", "applied"), cand["gate_json"])
        required = json.loads(cand["required_cases_json"])
        self.assertEqual(set(required), {"runner-reports", "reporter-reports", "skill-negative"})
        gate = json.loads(cand["gate_json"])
        self.assertEqual(gate["decision"], "accept")
        self.assertEqual(gate["checks"]["target_effect"], "pass")
        self.assertEqual(self.jobs("evaluate")[0]["state"], "succeeded")
        self.assertEqual(self.jobs("apply")[0]["state"], "succeeded")
        runs = self.runs(cid)
        self.assertEqual(len(runs), 2 * len(required))
        self.assertEqual({r["arm"] for r in runs}, {"baseline", "candidate"})

        # Apply: one commit with the trailer, a fast-forward of main.
        commit = cand["candidate_commit"]
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), commit)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), self.base)
        self.assertEqual(git(self.repo, "log", "--format=%H", f"--grep=Autotune-Candidate: {cid}", "main").split(),
                         [commit])
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{self.base}..HEAD"), "1")

        # The merged skill: source gone, script moved with its bit, links resolve,
        # both exceptions kept.
        self.assertFalse((self.repo / REPORTER_DIR).exists())
        script = self.repo / RUNNER_DIR / RENAMED
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)
        self.assertEqual(script.read_text(), SKILL_FILES[f"{REPORTER_DIR}/scripts/report.sh"][0])
        merged = skills.by_path(skills.inventory(self.repo))[RUNNER_DIR]
        self.assertTrue(merged.links and all(link.resolves for link in merged.links), merged.links)
        self.assertIn("Never hide a skipped test count.", merged.body)
        self.assertIn("Do not report success unless the run printed OK.", merged.body)
        self.assertTrue((self.repo / AUDIT_DIR / "SKILL.md").exists())

        # The checkout changed only inside the two skill directories.
        end = snapshot(self.repo)
        changed = {p for p in start["files"].keys() | end["files"].keys()
                   if start["files"].get(p) != end["files"].get(p)}
        self.assertTrue(changed)
        self.assertTrue(all(p.startswith((RUNNER_DIR + "/", REPORTER_DIR + "/")) for p in changed), changed)
        self.assertEqual(end["status"], "")

        # A merge row links the removed source to the candidate; Git holds the old body.
        merges = [tuple(r) for r in self.conn.execute(
            "SELECT path, candidate_id FROM skill_events WHERE event = 'merge'")]
        self.assertEqual(merges, [(REPORTER_DIR, cid)])
        self.assertEqual(git(self.repo, "show", f"{self.base}:{REPORTER_DIR}/SKILL.md") + "\n", REPORTER)
        # No separate skill history: a fixed table set, and no row holds a
        # line of the old body (a JSON-escaped copy included).
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables - {"sqlite_sequence"}, set(STORE_TABLES))
        self.assertIn(OLD_BODY_LINE, REPORTER)
        for table in sorted(tables):
            for row in self.conn.execute(f"SELECT * FROM {table}"):
                self.assertNotIn(OLD_BODY_LINE, "\0".join(str(v) for v in row), table)

        # A second review of the same pair at the new HEAD finds nothing to merge.
        self.script(good_review())
        self.ingest_turn("turn-2")
        self.assertEqual(worker.run_worker(str(self.repo), idle_seconds=0.3, poll_seconds=0.05), 0)
        self.assertEqual(len(self.jobs("consolidate")), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)
        self.assertEqual(self.candidate(cid)["status"], "applied")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), commit)

        self._cycle3_commands(episode_id, cid)

    def _cycle3_commands(self, episode_id: str, cid: str) -> None:
        # consolidate: one sweep job, queued now.
        code, out, err = self.cli("consolidate")
        self.assertEqual(code, 0, err)
        sweeps = [j for j in self.jobs("consolidate") if json.loads(j["payload_json"]).get("mode") == "sweep"]
        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0]["state"], "queued")

        # calibrate without --live runs nothing.
        calls_before = len(self.fake_calls())
        code, out, _ = self.cli("calibrate", "--set", str(PLUGIN_ROOT / "calibration" / "seed"))
        self.assertEqual(code, 2)
        self.assertIn("--live", out)
        self.assertEqual(len(self.fake_calls()), calls_before)

        # mutations, simulated, on a small subset (one harmful, one useful, one
        # neutral): --set is honoured, nothing is falsely accepted, and the run
        # exits 1 only because a subset is below the release set size.
        with tempfile.TemporaryDirectory() as tmp:
            subset = Path(tmp)
            for name in ("_baseline.json", "broadened-trigger-01.json", "useful-01.json", "neutral-01.json"):
                shutil.copy(mutations.DEFAULT_SET_DIR / name, subset / name)
            code, out, err = self.cli("mutations", "--set", str(subset))
        self.assertEqual(code, 1, out + err)
        self.assertIn("Harmful: 1, false accepts 0", out)
        self.assertIn("harmful_set_size: not met", out)
        self.assertIn("Useful: 1 of 1 accepted", out)
        self.assertEqual(len(self.fake_calls()), calls_before)

        # export the reviewed episode as a sanitized fixture.
        out_file = self.root / "export.json"
        code, out, err = self.cli("export", episode_id, "--out", str(out_file))
        self.assertEqual(code, 0, err)
        exported = out_file.read_text()
        self.assertNotIn(str(self.repo), exported)
        self.assertEqual(stat.S_IMODE(os.stat(out_file).st_mode), 0o600)
        self.assertFalse(json.loads(exported)["synthetic"])
        # An export is the starting point of a real calibration example: it
        # loads as one (unlabelled, so it counts toward nothing yet).
        with tempfile.TemporaryDirectory() as tmp:
            example = json.loads(exported)
            shutil.copy(out_file, Path(tmp) / f"{example['id']}.json")
            [loaded] = calibrate.load_set(tmp)
            self.assertFalse(loaded["synthetic"])

        # uninstall: plan first, then remove data, branches and worktrees, never the checkout.
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        data_dir = paths.project_data_dir(common)
        before = snapshot(self.repo)
        code, out, err = self.cli("uninstall")
        self.assertEqual(code, 1, err)
        self.assertIn("--yes", out)
        self.assertIn(str(data_dir), out)
        self.assertTrue(data_dir.exists())
        self.assertEqual(snapshot(self.repo), before)
        self.conn.close()
        code, out, err = self.cli("uninstall", "--yes")
        self.assertEqual(code, 0, err)
        self.assertFalse(data_dir.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "autotune/*"), "")
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        after = snapshot(self.repo)
        self.assertEqual((after["files"], after["head"], after["status"]), (before["files"], before["head"], ""))
        self.assertTrue((self.repo / ".autotune.json").exists())
        self.assertIn(f"Autotune-Candidate: {cid}", git(self.repo, "log", "-1", "--format=%B"))


if __name__ == "__main__":
    unittest.main()
