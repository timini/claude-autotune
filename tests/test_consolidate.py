"""M6 exit criteria: skill consolidation through the ordinary candidate path.

Real temporary Git repos, the scripted fake CLI (curator, author and replay
responses), the real consolidate, evaluate and apply handlers and the real
gate. The evaluator code digest is pinned so edits elsewhere in the plugin
tree during a test run cannot make the identity check fail.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from unittest import mock

from autotune import cases, consolidate, queue, skills
from tests._cycle2_support import Cycle2Case, case_obj, git, snapshot
from tests.test_skills import (
    AUDIT, AUDIT_DIR, MERGED, REPORTER, REPORTER_DIR, RUNNER_DIR, SKILL_FILES, full_coverage, write_tree,
)

SUMMARY = {"summary": "Merged test-reporter into test-runner.", "changed_behavior": "One skill covers both.",
           "potential_regressions": ["The merged description is longer."]}
ANSWER = "Ran the tests: 1 FAILING. Report attached."
# The merge moves the source script to a new name and rewrites the link to it.
RENAMED = "scripts/reporter-report.sh"
RENAMED_MERGED = MERGED.replace("`scripts/report.sh`", f"`{RENAMED}`")


# Every table the store has (schema v3/v4). A table added to keep old skill
# bodies would show up here.
STORE_TABLES = ("meta", "episodes", "evidence", "foreground_activity", "capture_errors", "jobs", "model_calls",
                "reviews", "candidates", "evaluation_runs", "skill_events")
# A line of the source skill's body that is not an exception, so it is copied
# into no coverage map or merged body.
OLD_BODY_LINE = "Summarise failures with file and line."


def renamed_coverage() -> list[dict]:
    return [dict(e, destination=f"{RUNNER_DIR}/{RENAMED}") if e["kind"] == "support_file" else e
            for e in full_coverage()]


def skill_cases() -> list[dict]:
    reports = [{"id": "reports-failing", "type": "answer_contains", "text": "FAILING", "critical": False}]
    return [
        case_obj("skill-target", "target", reports, harness=(".claude/skills/**",), family="skills"),
        case_obj("skill-negative", "negative_trigger",
                 [{"id": "no-release-talk", "type": "answer_not_contains", "text": "release", "critical": True}],
                 critical=True, harness=(".claude/skills/**",), family="docs",
                 request="Fix the typo in README."),
        case_obj("claude-md-only", "protected", reports, critical=True, harness=("CLAUDE.md",)),
    ]


def decision(*, coverage=None, target=RUNNER_DIR, sources=(REPORTER_DIR,), kind="merge") -> dict:
    return {"decision": kind, "target": target, "sources": list(sources),
            "coverage_map": full_coverage() if coverage is None else coverage,
            "rationale": "Both skills run the unit tests and report failures.", "risks": ["Longer description."]}


class ConsolidationCase(Cycle2Case):
    repo_config = {"enabled": True, "executable_paths": [".claude/skills/*/scripts/*"]}
    cases = skill_cases()
    extra_cfg = {"critical_skills": [AUDIT_DIR]}

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(cases, "code_digest", return_value="0" * 64)
        patcher.start()
        self.addCleanup(patcher.stop)
        write_tree(self.repo, SKILL_FILES)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "skills")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.cfg = {**self.cfg, **self.extra_cfg}

    def curate(self, *responses):
        self.script(*responses, {"role": "replay", "result": ANSWER})
        return self.run_job("consolidate", None, payload={"mode": "sweep"}, dedup="consolidate:sweep:test",
                            cfg=self.cfg)

    def author(self, skill_md: str = MERGED, **edits) -> dict:
        return {"role": "author", "structured_output": SUMMARY, "result": "done",
                "edits": {f"{RUNNER_DIR}/SKILL.md": skill_md, **edits}}

    def roles(self) -> list[str]:
        return [c["env"]["AUTOTUNE_ORIGIN"] for c in self.fake_calls()]

    def branches(self) -> list[str]:
        return [b for b in git(self.repo, "branch", "--list", "autotune/*", "--format=%(refname:short)").split() if b]


class MergeAcceptedTest(ConsolidationCase):
    def test_merge_is_authored_gated_and_applied_as_one_commit(self):
        start = snapshot(self.repo)
        res, job = self.curate({"role": "worker", "structured_output": decision(coverage=renamed_coverage())},
                               self.author(RENAMED_MERGED))
        self.assertEqual(job["state"], "succeeded", job["last_error_detail"])
        cid = res.result["candidate_id"]
        cand = self.candidate(cid)
        self.assertEqual((cand["status"], cand["change_kind"]), ("validating", "consolidation"))
        self.assertEqual(json.loads(cand["allowed_paths_json"]), [f"{REPORTER_DIR}/**", f"{RUNNER_DIR}/**"])
        self.assertEqual(cand["hypothesis"], f"Merge {REPORTER_DIR} into {RUNNER_DIR} preserving all triggers, "
                                             "exceptions and support files")
        self.assertEqual(cand["effect_metric"], consolidate.EFFECT_METRIC)
        self.assertEqual(json.loads(cand["required_cases_json"]), ["skill-target", "skill-negative"])
        info = skills.consolidation_info(cand)
        self.assertEqual((info["target"], info["sources"]), (RUNNER_DIR, [REPORTER_DIR]))
        self.assertEqual(self.roles(), ["worker", "author"])
        self.assertEqual(snapshot(self.repo), start, "authoring never touches the user checkout")

        # The candidate commit: source removed, script moved with its bit, links resolve.
        commit = cand["candidate_commit"]
        tree = git(self.repo, "ls-tree", "-r", commit, "--", ".claude/skills")
        self.assertIn("100755 blob", next(line for line in tree.splitlines() if line.endswith(f"{RUNNER_DIR}/{RENAMED}")))
        self.assertNotIn(REPORTER_DIR, tree)
        self.assertNotIn(f"{RUNNER_DIR}/scripts/report.sh", tree)
        merged = skills.by_path(skills.inventory_at(self.repo, commit))[RUNNER_DIR]
        self.assertTrue(all(link.resolves for link in merged.links), merged.links)
        self.assertIn(RENAMED, [link.target for link in merged.links])
        self.assertEqual(skills.consolidation_info(cand)["moves"][0]["to"], f"{RUNNER_DIR}/{RENAMED}")

        # Curator and author prompts carry the skills as JSON DATA only.
        curator = self.fake_calls()[0]
        self.assertIn("--safe-mode", curator["argv"])
        self.assertEqual(curator["argv"][curator["argv"].index("--tools") + 1], "")
        data = json.loads(curator["stdin"].split("\nDATA\n", 1)[1])
        by = {s["path"]: s for s in data["skills"]}
        self.assertEqual(by[REPORTER_DIR]["extracted"]["exceptions"], ["Never hide a skipped test count."])

        res, job = self.run_job("evaluate", cid, cfg=self.cfg)
        cand = self.candidate(cid)
        self.assertEqual(cand["status"], "accepted", cand["gate_json"])
        gate_json = json.loads(cand["gate_json"])
        self.assertEqual(gate_json["checks"]["target_effect"], "pass")
        self.assertEqual(len(self.runs(cid)), 4)

        res, job = self.run_job("apply", cid, cfg=self.cfg)
        self.assertEqual(job["state"], "succeeded", job["last_error_detail"])
        cand = self.candidate(cid)
        self.assertEqual(cand["status"], "applied")
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(head, commit)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), self.base)
        trailers = git(self.repo, "log", "--format=%H", f"--grep=Autotune-Candidate: {cid}", "main").split()
        self.assertEqual(trailers, [commit])
        self.assertFalse((self.repo / REPORTER_DIR).exists())
        script = self.repo / RUNNER_DIR / RENAMED
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)
        self.assertEqual(script.read_text(), SKILL_FILES[f"{REPORTER_DIR}/scripts/report.sh"][0])
        self.assertEqual((self.repo / RUNNER_DIR / "SKILL.md").read_text(), RENAMED_MERGED)
        merges = self.conn.execute(
            "SELECT path, candidate_id FROM skill_events WHERE event = 'merge'").fetchall()
        self.assertEqual([tuple(m) for m in merges], [(REPORTER_DIR, cid)])

        # No separate skill history: the schema has no new table and the old
        # skill body is stored nowhere in the data dir; Git holds it.
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables - {"sqlite_sequence"}, set(STORE_TABLES))
        # One distinctive line of the old body, so a JSON-escaped copy is found too.
        self.assertIn(OLD_BODY_LINE, REPORTER)
        self.assertNotIn(OLD_BODY_LINE.encode(), self._data_bytes())
        self.assertEqual(git(self.repo, "show", f"{self.base}:{REPORTER_DIR}/SKILL.md") + "\n", REPORTER)

    def _data_bytes(self) -> bytes:
        out = b""
        for dirpath, dirnames, filenames in os.walk(self.data_dir):
            dirnames[:] = [d for d in dirnames if d != "worktrees"]
            for name in filenames:
                out += (Path(dirpath) / name).read_bytes()
        conn = sqlite3.connect(self.db_path)
        try:
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                for row in conn.execute(f"SELECT * FROM {table}"):
                    out += "\0".join(str(v) for v in row).encode()
        finally:
            conn.close()
        return out


class MergeRejectedTest(ConsolidationCase):
    def test_coverage_map_missing_an_exception_is_rejected_before_any_edit(self):
        coverage = [e for e in full_coverage() if e["kind"] != "exception"]
        res, job = self.curate({"role": "worker", "structured_output": decision(coverage=coverage)}, self.author())
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(res.result["rejected"], "coverage_incomplete")
        self.assertEqual(res.result["missing"], [{"source": REPORTER_DIR, "kind": "exception",
                                                  "item": "Never hide a skipped test count."}])
        self.assertEqual(self.roles(), ["worker"], "the author never runs")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.branches(), [])

    def test_concise_merge_that_drops_an_exception_is_rejected_by_validate_merge(self):
        # The curator's map claims the exception is kept; the edit drops it.
        concise = MERGED.replace("- Never hide a skipped test count.\n", "")
        res, job = self.curate({"role": "worker", "structured_output": decision()}, self.author(concise))
        self.assertEqual(job["state"], "succeeded", job["last_error_detail"])
        cand = self.candidate(res.result["candidate_id"])
        self.assertEqual((cand["status"], cand["reason"]), ("rejected", "lost_exception"))
        violations = json.loads(cand["validation_json"])["violations"]
        self.assertEqual(violations, [{"code": "lost_exception", "path": REPORTER_DIR,
                                       "detail": "Never hide a skipped test count."}])
        self.assertIsNone(cand["candidate_commit"])
        self.assertEqual(self.branches(), [])
        self.assertEqual(self.jobs("evaluate"), [])

    def test_broken_link_after_the_edit_is_rejected(self):
        body = MERGED.replace("`scripts/report.sh`", "`../test-reporter/scripts/report.sh`")
        res, _ = self.curate({"role": "worker", "structured_output": decision()}, self.author(body))
        self.assertEqual(self.candidate(res.result["candidate_id"])["reason"], "broken_link")

    def test_link_not_rewritten_after_a_rename_is_rejected(self):
        res, _ = self.curate({"role": "worker", "structured_output": decision(coverage=renamed_coverage())},
                             self.author(MERGED))
        cand = self.candidate(res.result["candidate_id"])
        self.assertEqual((cand["status"], cand["reason"]), ("rejected", "broken_link"))
        self.assertEqual(json.loads(cand["validation_json"])["violations"][0]["detail"], "scripts/report.sh")

    def test_merge_that_broadens_a_trigger_is_rejected_by_the_gate(self):
        # The static checks pass, but with the merged skill the replay of the
        # required negative-trigger case strays into release work.
        res, _ = self.curate({"role": "worker", "structured_output": decision()}, self.author())
        cid = res.result["candidate_id"]
        self.script({"role": "replay", "when_file_contains": {"path": f"{RUNNER_DIR}/SKILL.md", "text": "tests or for"},
                     "result": "Ran the tests: 1 FAILING. Also checked the release."},
                    {"role": "replay", "result": ANSWER})
        self.run_job("evaluate", cid, cfg=self.cfg)
        cand = self.candidate(cid)
        self.assertEqual(cand["status"], "rejected", cand["gate_json"])
        self.assertIn(cand["reason"], ("critical_regression", "regression_observed"))


class CriticalSkillTest(ConsolidationCase):
    extra_cfg = {"critical_skills": [AUDIT_DIR, RUNNER_DIR]}

    def test_zero_use_critical_skill_is_never_a_merge_source(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM skill_events").fetchone()[0], 0)
        res, job = self.curate({"role": "worker", "structured_output": decision(target=REPORTER_DIR,
                                                                                sources=(RUNNER_DIR,))},
                               self.author())
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(res.result["rejected"], "protected_source")
        self.assertEqual(self.roles(), ["worker"])
        self.assertTrue((self.repo / RUNNER_DIR / "SKILL.md").exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        curator = json.loads(self.fake_calls()[0]["stdin"].split("\nDATA\n", 1)[1])
        self.assertTrue({s["path"]: s for s in curator["skills"]}[RUNNER_DIR]["critical"])

    def test_two_protected_skills_are_never_paired(self):
        write_tree(self.repo, {f"{AUDIT_DIR}/SKILL.md": (AUDIT.replace(
            "Audit a release before tagging it. Use when the user prepares a release.",
            "Run the project unit tests and report failing tests. Use when the user asks to run tests."), False)})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "audit overlaps runner")
        cfg = {**self.cfg, "pinned_skills": [REPORTER_DIR]}
        self.script({"role": "worker", "structured_output": decision()})
        res, job = self.run_job("consolidate", None, payload={"mode": "sweep"}, dedup="consolidate:sweep:p",
                                cfg=cfg)
        self.assertEqual(res.result, {"skipped": "no_overlap", "mode": "sweep"})
        self.assertEqual(self.fake_calls(), [])


class HandlerEdgeTest(ConsolidationCase):
    def test_no_change_and_no_overlap_create_nothing(self):
        res, job = self.curate({"role": "worker", "structured_output": decision(kind="no_change", sources=())})
        self.assertEqual(res.result["skipped"], "no_change")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (skills.LAST_SWEEP_META,)).fetchone())
        cfg = {**self.cfg, "consolidation_overlap_threshold": 0.99}
        res, _ = self.run_job("consolidate", None, payload={"mode": "sweep"}, dedup="consolidate:sweep:hi", cfg=cfg)
        self.assertEqual(res.result["skipped"], "no_overlap")

    def test_a_curated_pair_is_not_curated_again_at_the_same_head(self):
        third = RUNNER_DIR + "-fast"
        write_tree(self.repo, {f"{third}/SKILL.md": (
            "---\nname: fast-tests\ndescription: Run the project unit tests quickly. Use when the user asks to run "
            "tests fast.\n---\nRun the fast suite.\n", False)})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "third skill")
        pairs = skills.overlaps(skills.inventory_at(self.repo, "HEAD", self.cfg), skills.threshold(self.cfg))
        self.assertGreaterEqual(len(pairs), 2)
        seen = []
        for i in range(len(pairs)):
            self.script({"role": "worker", "structured_output": decision(kind="no_change", sources=())})
            res, job = self.run_job("consolidate", None, payload={"mode": "sweep"},
                                    dedup=f"consolidate:sweep:{i}", cfg=self.cfg)
            self.assertEqual(res.result["skipped"], "no_change")
            seen.append(tuple(res.result["skills"]))
        # Each sweep moved on to the next pair instead of asking about the first again.
        self.assertEqual(seen, [(a, b) for a, b, _, _ in pairs])
        calls = len(self.fake_calls())
        res, _ = self.run_job("consolidate", None, payload={"mode": "sweep"}, dedup="consolidate:sweep:last",
                              cfg=self.cfg)
        self.assertEqual(res.result, {"skipped": "prior_attempt", "mode": "sweep", "pairs": len(pairs)})
        self.assertEqual(len(self.fake_calls()), calls, "no curator call for pairs already decided at this HEAD")
        # A rejection is remembered too, and a new HEAD makes the pairs eligible again.
        write_tree(self.repo, {"CLAUDE.md": ("new\n", False)})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "new head")
        self.script({"role": "worker", "structured_output": decision(coverage=[])})
        res, _ = self.run_job("consolidate", None, payload={"mode": "sweep"}, dedup="consolidate:sweep:new",
                              cfg=self.cfg)
        self.assertEqual(res.result["rejected"], "coverage_incomplete")
        self.script({"role": "worker", "structured_output": decision(kind="no_change", sources=())})
        res, _ = self.run_job("consolidate", None, payload={"mode": "sweep"}, dedup="consolidate:sweep:new2",
                              cfg=self.cfg)
        self.assertNotEqual(tuple(res.result.get("skills") or ()), (REPORTER_DIR, RUNNER_DIR))

    def test_busy_candidate_slot_defers(self):
        self.make_candidate({"CLAUDE.md": "x\n"}, status="evaluating")
        res, job = self.curate({"role": "worker", "structured_output": decision()})
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "candidate_slot_busy"))
        self.assertEqual(self.fake_calls(), [])

    def test_dirty_skill_directory_defers_without_touching_it(self):
        (self.repo / REPORTER_DIR / "SKILL.md").write_text(REPORTER + "local edit\n")
        before = snapshot(self.repo)
        res, job = self.curate({"role": "worker", "structured_output": decision()}, self.author())
        self.assertEqual((job["state"], job["last_error"]), ("retry_wait", "checkout_dirty"))
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_pair_mode_rechecks_the_named_skills(self):
        self.script({"role": "worker", "structured_output": decision(kind="no_change", sources=())})
        res, _ = self.run_job("consolidate", None, payload={"mode": "pair", "skills": [REPORTER_DIR, "nope"]},
                              dedup="consolidate:pair:x", cfg=self.cfg)
        self.assertEqual(res.result["skipped"], "skill_missing")
        res, _ = self.run_job("consolidate", None, payload={"mode": "pair", "skills": [REPORTER_DIR, RUNNER_DIR]},
                              dedup="consolidate:pair:y", cfg=self.cfg)
        self.assertEqual(res.result["skipped"], "no_change")

    def test_error_codes_are_classified(self):
        self.assertEqual([c for c in consolidate.ERROR_CODES if c not in queue._CODE_CLASSES], [])


if __name__ == "__main__":
    import unittest

    unittest.main()
