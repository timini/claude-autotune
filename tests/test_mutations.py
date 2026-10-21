"""Gate mutation set (M7): the simulated suite against the real static checks
and the real gate, plus a deliberately weakened gate that must show up as a
false accept. No model is called."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from autotune import cases, config, gate, mutations

ROOT = Path(__file__).resolve().parent.parent
SET_DIR = ROOT / "benchmarks" / "mutations"
HARNESS = ROOT / "benchmarks" / "fixture-harness"


def subset(tmp: Path, ids: list[str]) -> Path:
    """A set directory with the baseline file and the named mutations."""
    out = tmp / "set"
    out.mkdir()
    shutil.copy(SET_DIR / mutations.BASELINE_FILE, out / mutations.BASELINE_FILE)
    for mid in ids:
        shutil.copy(SET_DIR / f"{mid}.json", out / f"{mid}.json")
    return out


class SetCompositionTest(unittest.TestCase):
    def test_counts_meet_the_contract(self):
        loaded, baseline = mutations.load_set(SET_DIR)
        comp = mutations.composition(loaded)
        self.assertEqual(comp["harmful"], 40)
        self.assertEqual(comp["useful"], 9)
        self.assertEqual(comp["neutral"], 6)
        for category in mutations.HARMFUL_CATEGORIES:
            self.assertGreaterEqual(comp["by_category"][category], 3, category)
        self.assertTrue(comp["ok"])
        self.assertTrue(baseline)

    def test_every_harmful_mutation_names_the_check_that_catches_it(self):
        loaded, _ = mutations.load_set(SET_DIR)
        for m in loaded:
            if m["category"] in mutations.HARMFUL_CATEGORIES:
                self.assertTrue(m["detected_by"], m["id"])
            if m["category"] in mutations.STATIC_ONLY_CATEGORIES:
                self.assertEqual(m["simulated_runs"], {}, m["id"])

    def test_no_em_or_en_dashes(self):
        files = [ROOT / "autotune" / "mutations.py", Path(__file__)]
        files += [p for p in SET_DIR.rglob("*") if p.is_file()]
        files += [p for p in HARNESS.rglob("*") if p.is_file()]
        for path in files:
            if path.name == ".DS_Store" or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(chr(0x2014), text, path)
            self.assertNotIn(chr(0x2013), text, path)

    def test_malformed_mutations_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = {"id": "bad-one", "category": "useful", "expected": "reject_or_abstain",
                   "patch": {"CLAUDE.md": "x"}}
            (root / "bad-one.json").write_text(json.dumps(bad))
            with self.assertRaises(mutations.MutationError) as ctx:
                mutations.load_set(root)
            self.assertEqual(ctx.exception.code, "invalid_mutation")
            bad.update(expected="accept", patch={"../escape.md": "x"})
            (root / "bad-one.json").write_text(json.dumps(bad))
            with self.assertRaises(mutations.MutationError):
                mutations.load_set(root)


class FixtureHarnessTest(unittest.TestCase):
    def test_policy_cases_and_modes(self):
        cfg = config.load_config(HARNESS)
        self.assertIn(".claude/settings.json", cfg["field_rules"])
        for rel in (".claude/hooks/guard.sh", ".claude/skills/release-notes/scripts/collect.sh",
                    ".claude/skills/db-migrate/scripts/check_migrations.sh"):
            self.assertTrue(os.stat(HARNESS / rel).st_mode & stat.S_IXUSR, rel)
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = mutations.build_repo(HARNESS, Path(os.path.realpath(tmp)))
            loaded, errors = cases.load_cases(repo, base)
        self.assertEqual(errors, [])
        kinds = {c.kind for c in loaded}
        self.assertEqual(kinds, {"target", "generalization", "protected", "negative_trigger", "general"})
        self.assertTrue(any(c.kind == "protected" and c.critical for c in loaded))


class SimulatedSuiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "report.json"
        cls.report = mutations.run(SET_DIR, "simulated", out_path=cls.out)
        cls.by_id = {r["id"]: r for r in cls.report["results"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_release_criterion_is_met(self):
        r = self.report
        self.assertTrue(r["passed"], mutations.summary(r))
        self.assertEqual(r["harmful"]["false_accepts"], 0)
        self.assertEqual(r["harmful"]["total"], 40)
        self.assertGreaterEqual(r["useful"]["accepted_rate"], 0.8)
        self.assertEqual(r["neutral"]["reported_as_improvement"], 0)
        self.assertAlmostEqual(r["harmful"]["false_accept_rate_upper"], 0.0881, places=4)
        for name, criterion in r["criteria"].items():
            self.assertTrue(criterion["met"], name)

    def test_each_mutation_is_caught_by_the_expected_check(self):
        for r in self.report["results"]:
            self.assertTrue(r["meets_expectation"], r)
            self.assertTrue(r["detected_as_expected"], r)
            self.assertEqual(r.get("ignored_overrides", []), [], r["id"])
        self.assertEqual(self.report["detection_misses"], [])

    def test_abstentions_and_rejections_are_reported_separately(self):
        h = self.report["harmful"]
        self.assertEqual(h["rejected"] + h["abstained"], h["total"])
        self.assertIn("broadened-trigger-03", h["abstained_ids"])
        self.assertEqual(self.by_id["broadened-trigger-03"]["decision"], "inconclusive")
        self.assertEqual(h["rejected_static"] + h["rejected_gate"], h["rejected"])

    def test_static_categories_are_caught_with_no_runs(self):
        for r in self.report["results"]:
            if r["category"] in mutations.STATIC_ONLY_CATEGORIES:
                self.assertEqual(r["stage"], "static", r["id"])
                self.assertEqual(r["required_cases"], [], r["id"])
        self.assertIn("protected_path", self.by_id["changed-grader-01"]["reason_codes"])
        self.assertIn("executable_bit", self.by_id["new-executable-02"]["reason_codes"])

    def test_consolidation_mutations_use_the_skill_merge_check(self):
        for mid in ("lost-exception-03", "lost-exception-04", "broken-reference-03", "broken-reference-04"):
            r = self.by_id[mid]
            self.assertEqual(r["stage"], "static", mid)
            self.assertEqual(r["skill_check"], "skills.validate_merge")

    def test_neutral_changes_are_not_improvements(self):
        for r in self.report["results"]:
            if r["category"] == "neutral":
                self.assertNotEqual(r["decision"], "accept", r["id"])
                self.assertTrue(set(r["reason_codes"]) & {"target_not_met", "outcomes_not_equivalent"}, r["id"])

    def test_consolidations_reach_the_gate_consolidation_rule(self):
        self.assertEqual((self.by_id["useful-09"]["decision"], self.by_id["useful-09"]["stage"]), ("accept", "gate"))
        self.assertEqual(self.by_id["neutral-05"]["reason_codes"], ["target_not_met"])
        self.assertEqual(self.by_id["neutral-06"]["reason_codes"], ["outcomes_not_equivalent"])
        for mid in ("useful-09", "neutral-05", "neutral-06"):
            self.assertEqual(self.by_id[mid]["change_kind"], "consolidation", mid)
            self.assertEqual(self.by_id[mid]["skill_check"], "skills.validate_merge", mid)

    def test_report_file_is_written(self):
        saved = json.loads(self.out.read_text())
        self.assertEqual(saved["passed"], self.report["passed"])
        self.assertTrue(saved["synthetic"])
        self.assertEqual(len(saved["results"]), 55)


class WeakenedGateTest(unittest.TestCase):
    def test_disabled_known_successes_check_shows_a_false_accept(self):
        ids = ["broadened-trigger-01", "useful-01", "neutral-01"]
        with tempfile.TemporaryDirectory() as tmp:
            set_dir = subset(Path(tmp), ids)
            honest = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
            self.assertEqual(honest["harmful"]["false_accepts"], 0)
            with mock.patch.object(gate, "_known_successes", lambda res, cases, slots: None):
                weak = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
        self.assertEqual(weak["harmful"]["false_accepts"], 1)
        self.assertEqual(weak["harmful"]["false_accept_ids"], ["broadened-trigger-01"])
        self.assertFalse(weak["criteria"]["harmful_false_accepts_zero"]["met"])
        self.assertFalse(weak["passed"])
        self.assertGreater(weak["harmful"]["false_accept_rate_upper"], honest["harmful"]["false_accept_rate_upper"])
        self.assertIn("False accepts: broadened-trigger-01", mutations.summary(weak))

    def test_disabled_static_check_shows_a_false_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_dir = subset(Path(tmp), ["weakened-tool-restriction-03"])
            ok = mock.Mock(violations=[], needs_scope_change=False, ok=True)
            with mock.patch.object(mutations.scope, "validate_changes", return_value=ok):
                weak = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
        self.assertEqual(weak["harmful"]["false_accept_ids"], ["weakened-tool-restriction-03"])


    def test_disabled_merge_check_fails_the_run(self):
        ids = ["lost-exception-03", "lost-exception-04", "broken-reference-03", "broken-reference-04"]
        with tempfile.TemporaryDirectory() as tmp:
            set_dir = subset(Path(tmp), ids)
            honest = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
            with mock.patch.object(mutations.skills, "validate_merge", return_value=[]):
                weak = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
        self.assertTrue(honest["criteria"]["caught_by_the_named_check"]["met"])
        # The gate still rejects them on the hand-written runs, so there is no
        # false accept; the named check missing them is what fails the run.
        self.assertEqual(weak["harmful"]["false_accepts"], 0)
        self.assertFalse(weak["criteria"]["caught_by_the_named_check"]["met"])
        self.assertEqual(sorted(weak["criteria"]["caught_by_the_named_check"]["missed"]), sorted(ids))

    def test_disabled_consolidation_rule_fails_the_run(self):
        ids = ["useful-09", "neutral-05", "neutral-06"]
        with tempfile.TemporaryDirectory() as tmp:
            set_dir = subset(Path(tmp), ids)
            honest = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
            with mock.patch.object(gate, "_consolidation_effect", lambda res, inp, cases, slots: None):
                weak = mutations.run(set_dir, "simulated", harness_dir=HARNESS)
        self.assertTrue(honest["criteria"]["neutral_not_reported_as_improvement"]["met"])
        self.assertEqual(honest["useful"]["accepted"], 1)
        self.assertFalse(weak["criteria"]["neutral_not_reported_as_improvement"]["met"])
        self.assertEqual(sorted(weak["neutral"]["reported_as_improvement_ids"]), ["neutral-05", "neutral-06"])
        self.assertFalse(weak["criteria"]["caught_by_the_named_check"]["met"])


class TrialExpansionTest(unittest.TestCase):
    def setUp(self):
        self.case = SimpleNamespace(id="c", graders=[{"id": "soft", "critical": False},
                                                     {"id": "hard", "critical": True}])

    def test_named_outcomes(self):
        self.assertEqual(mutations.expand_trial(self.case, "success", "w")[1],
                         [{"id": "soft", "status": "pass"}, {"id": "hard", "status": "pass"}])
        outcome, checks, _ = mutations.expand_trial(self.case, "constraint_violation", "w")
        self.assertEqual((outcome, checks[1]["status"]), ("constraint_violation", "fail"))
        outcome, checks, _ = mutations.expand_trial(self.case, "task_failure", "w")
        self.assertEqual((outcome, checks[0]["status"]), ("task_failure", "fail"))
        self.assertEqual(mutations.expand_trial(self.case, "timeout", "w")[:2], ("timeout", []))
        self.assertEqual(mutations.expand_trial(self.case, {"fail": ["hard"], "duration_ms": 5}, "w")[2], 5)

    def test_unknown_grader_or_wrong_trial_count_is_refused(self):
        with self.assertRaises(mutations.MutationError):
            mutations.expand_trial(self.case, {"fail": ["missing"]}, "w")
        with self.assertRaises(mutations.MutationError):
            mutations.expand_trial(self.case, "passed", "w")
        with self.assertRaises(mutations.MutationError):
            mutations._trial_specs(["success"], 2, "w")


class ClopperPearsonTest(unittest.TestCase):
    def test_known_values(self):
        # Two-sided 95% exact intervals: 0/40 upper 1 - 0.025**(1/40); 1/10 and 5/10 from published tables.
        self.assertAlmostEqual(mutations.clopper_pearson_upper(0, 40), 1 - 0.025 ** (1 / 40), places=10)
        self.assertAlmostEqual(mutations.clopper_pearson_upper(0, 40), 0.0881, places=4)
        self.assertAlmostEqual(mutations.clopper_pearson_upper(1, 10), 0.4450, places=4)
        self.assertAlmostEqual(mutations.clopper_pearson_upper(5, 10), 0.8129, places=4)
        self.assertEqual(mutations.clopper_pearson_upper(3, 3), 1.0)
        self.assertEqual(mutations.clopper_pearson_upper(0, 0), 1.0)
        self.assertAlmostEqual(mutations.clopper_pearson_upper(0, 40, one_sided=True), 0.0722, places=4)


class LivePathWithFakeCliTest(unittest.TestCase):
    """The live path (real evaluate handler, queue and worker outcome
    recording) driven by tests/fake_claude.py. No real model is reached."""

    def test_live_path_runs_the_evaluate_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            set_dir = subset(root, ["useful-01", "changed-grader-01"])
            script = root / "script.json"
            script.write_text(json.dumps({"responses": [
                {"role": "replay", "result": "Ran 2 tests. FAILED (failures=1)."}]}))
            env = {"AUTOTUNE_HOME": str(root / "home"), "AUTOTUNE_NO_WORKER": "1",
                   "AUTOTUNE_CLAUDE": str(ROOT / "tests" / "fake_claude.py"),
                   "AUTOTUNE_FAKE_SCRIPT": str(script), "AUTOTUNE_FAKE_LOG": str(root / "fake.log")}
            with mock.patch.dict(os.environ, env):
                for key in ("ANTHROPIC_API_KEY", "AUTOTUNE_ORIGIN"):
                    os.environ.pop(key, None)
                report = mutations.run(set_dir, "live", harness_dir=HARNESS, budget_calls=100)
            calls = [json.loads(line) for line in (root / "fake.log").read_text().splitlines() if line.strip()]
        by_id = {r["id"]: r for r in report["results"]}
        self.assertEqual(by_id["changed-grader-01"]["stage"], "static")
        # The fake answers the same in both arms, so the target is not met.
        self.assertEqual(by_id["useful-01"]["stage"], "gate")
        self.assertNotEqual(by_id["useful-01"]["decision"], "accept")
        self.assertTrue(calls)
        self.assertEqual(report["mode"], "live")


class EntryPointTest(unittest.TestCase):
    def test_live_without_budget_runs_nothing(self):
        err = io.StringIO()
        with mock.patch.object(mutations, "run") as run, redirect_stderr(err):
            code = mutations.main_args(SimpleNamespace(set=None, live=True, budget_calls=None, out=None))
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("--budget-calls", err.getvalue())
        with self.assertRaises(mutations.MutationError):
            mutations.run(SET_DIR, "live")

    def test_simulated_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_dir = subset(Path(tmp), ["useful-01", "neutral-01", "lost-exception-01"])
            out = io.StringIO()
            with mock.patch.object(mutations, "DEFAULT_SET_DIR", set_dir), redirect_stdout(out):
                # A small subset misses the 40-mutation size criterion, so the run reports a failure.
                code = mutations.main_args(SimpleNamespace(set=str(set_dir), live=False, budget_calls=None,
                                                           out=str(Path(tmp) / "r.json")))
            self.assertEqual(code, 1)
            self.assertIn("harmful_set_size: not met", out.getvalue())
            self.assertTrue((Path(tmp) / "r.json").exists())

    def test_bad_set_dir_is_a_usage_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = mutations.main_args(SimpleNamespace(set="/nonexistent/set", live=False, budget_calls=None))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
