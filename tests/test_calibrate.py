"""Calibration runner: seed set, statistics and threshold logic.

The end-to-end tests go through the real adapter and the fake CLI with
scripted reviewer and judge outputs. The large-sample threshold tests use a
stub adapter that answers by example id, since 120 fake processes add
nothing the fake-CLI tests do not already cover.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autotune import calibrate, claude_cli, judge, jsonschema_lite

from tests._adapter_support import FAKE_CLAUDE, REPO_ROOT, AdapterTestCase, FakeClock, make_cfg

SEED_DIR = REPO_ROOT / "calibration" / "seed"
SEED = calibrate.load_set(SEED_DIR)
BY_ID = {ex["id"]: ex for ex in SEED}


def review_output(ex: dict, *, scores: dict | None = None, cite: str = "e0001") -> dict:
    """A review whose scores equal the human labels unless overridden."""
    want = dict(ex["labels"]["dimensions"])
    want.update(scores or {})
    dims = {}
    for d in calibrate.DIMENSIONS:
        s = want.get(d)
        dims[d] = {"score": s, "reason": f"{d} judged from the trace.", "evidence_ids": [cite] if s is not None else []}
    return {
        "episode_id": ex["id"],
        "task_stage": "claimed_complete" if ex["labels"]["pass"] else "unknown",
        "dimensions": dims,
        "strengths": [],
        "improvements": [],
        "uncertainties": [],
        "recommendation": "no_change",
    }


def judge_output(label: str) -> dict:
    if label == "tie":
        return {"preference": "tie", "reasons": ["Equivalent results."], "cited": ["A.answer", "B.answer"]}
    return {"preference": label, "reasons": [f"Run {label} passes the required check."], "cited": [f"{label}.answer"]}


def judge_outputs(ex: dict, expected: str | None = None) -> list[dict]:
    """The two judge outputs, in call order, that map to `expected` (an
    example's own expected label by default) under both display orders."""
    expected = expected or ex["pairwise"]["expected"]
    first = judge.first_order(ex["id"])
    out = []
    for order in (first, "BA" if first == "AB" else "AB"):
        if expected == "tie":
            out.append(judge_output("tie"))
        else:
            arm = "baseline" if expected == "A" else "candidate"
            label = "A" if judge._ORDERS[order]["A"] == arm else "B"
            out.append(judge_output(label))
    return out


def scripted(examples, overrides=None) -> list[dict]:
    """Fake CLI responses for examples in load order. overrides maps an
    example id to a list of structured outputs to use instead."""
    overrides = overrides or {}
    responses = []
    for ex in examples:
        if ex["id"] in overrides:
            outs = overrides[ex["id"]]
        elif ex["kind"] == "review":
            outs = [review_output(ex)]
        else:
            outs = judge_outputs(ex)
        responses.extend({"structured_output": o, "result": "ok"} for o in outs)
    return responses


class SeedSetTests(unittest.TestCase):
    def test_seed_covers_every_probe_and_is_marked_synthetic(self):
        self.assertGreaterEqual(len(SEED), 14)
        self.assertLessEqual(len(SEED), 20)
        self.assertEqual({ex["probe"] for ex in SEED if ex["probe"]}, set(calibrate.PROBES))
        self.assertTrue(all(ex["synthetic"] is True for ex in SEED))
        self.assertEqual({ex["kind"] for ex in SEED}, {"review", "pairwise"})
        for ex in SEED:
            self.assertIn("Not calibration evidence", ex["note"])

    def test_every_seed_file_validates_and_matches_its_id(self):
        schema = calibrate.example_schema()
        for f in SEED_DIR.glob("*.json"):
            ex = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(jsonschema_lite.validate(ex, schema), [], f.name)
            self.assertEqual(f.stem, ex["id"])

    def test_no_dashes_in_calibration_files(self):
        for f in [*(REPO_ROOT / "calibration").rglob("*"), REPO_ROOT / "autotune" / "calibrate.py",
                  REPO_ROOT / "autotune" / "stats.py"]:
            if f.is_file():
                text = f.read_text(encoding="utf-8")
                self.assertNotIn(chr(0x2014), text, f)
                self.assertNotIn(chr(0x2013), text, f)

    def test_review_data_has_build_input_keys(self):
        data = calibrate.review_data(BY_ID["seed-review-success"])
        self.assertEqual(set(data), {"episode_id", "end_reason", "duration_ms", "model", "repository",
                                     "loaded_harness", "coverage", "input_limits", "evidence", "related_episodes",
                                     "known_task_families"})
        self.assertEqual(data["input_limits"]["sent_ids"], [e["id"] for e in data["evidence"]])


class LoadSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autotune-calset-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, obj):
        (self.tmp / name).write_text(json.dumps(obj), encoding="utf-8")

    def test_missing_and_empty_sets(self):
        with self.assertRaises(calibrate.SetError):
            calibrate.load_set(self.tmp / "nope")
        with self.assertRaises(calibrate.SetError):
            calibrate.load_set(self.tmp)

    def test_invalid_example_is_reported_with_file_name(self):
        bad = copy.deepcopy(BY_ID["seed-review-success"])
        bad["evidence"][0]["id"] = "x1"
        self.write("bad.json", bad)
        with self.assertRaises(calibrate.SetError) as cm:
            calibrate.load_set(self.tmp)
        self.assertTrue(any(e.startswith("bad.json:") for e in cm.exception.errors), cm.exception.errors)

    def test_duplicates_and_labeller_rule(self):
        ex = copy.deepcopy(BY_ID["seed-review-success"])
        self.write("a.json", ex)
        self.write("b.json", ex)
        real = copy.deepcopy(ex)
        real["id"] = "real-one"
        real["synthetic"] = False
        real["evidence"].append(dict(real["evidence"][0]))
        self.write("c.json", real)
        with self.assertRaises(calibrate.SetError) as cm:
            calibrate.load_set(self.tmp)
        text = "\n".join(cm.exception.errors)
        self.assertIn("duplicate example id", text)
        self.assertIn("duplicate evidence ids: e0001", text)
        self.assertIn("at least two labellers", text)

    def test_split_filter(self):
        ex = copy.deepcopy(BY_ID["seed-review-success"])
        dev = copy.deepcopy(ex)
        dev["id"] = "dev-one"
        dev["split"] = "rubric_development"
        self.write("a.json", ex)
        self.write("b.json", dev)
        self.assertEqual([e["id"] for e in calibrate.load_set(self.tmp)], [ex["id"]])
        self.assertEqual([e["id"] for e in calibrate.load_set(self.tmp, split="rubric_development")], ["dev-one"])
        self.assertEqual(len(calibrate.load_set(self.tmp, split=None)), 2)


class SeedRunFakeCliTests(AdapterTestCase):
    """The seed set through the real adapter and the fake CLI."""

    def adapter(self):
        return claude_cli.Adapter(executable=str(FAKE_CLAUDE), cfg=make_cfg(), data_dir=self.data_dir,
                                  base_env=self.base_env, clock=FakeClock())

    def run_seed(self, overrides=None):
        self.write_script(scripted(SEED, overrides))
        out = self.tmp / "out" / "report.json"
        report = calibrate.run(SEED_DIR, self.adapter(), make_cfg(), out, conn_factory=self.conn_factory,
                               data_dir=self.data_dir, now=FakeClock()())
        return report, out

    def test_perfect_scripted_outputs(self):
        report, out = self.run_seed()
        m = report["metrics"]
        n_review = sum(1 for ex in SEED if ex["kind"] == "review")
        n_pair = len(SEED) - n_review
        self.assertEqual(report["counts"]["examples"], len(SEED))
        self.assertEqual(report["counts"]["synthetic"], len(SEED))
        self.assertEqual(report["counts"]["adjudicated_non_synthetic"], 0)
        # Labelled: adjudicated, not ambiguous, human pass not null.
        labelled = [ex for ex in SEED if ex["kind"] == "review" and ex["labels"]["pass"] is not None
                    and not ex["labels"]["ambiguous"]]
        self.assertEqual(m["pass_fail_agreement"]["agree"], len(labelled))
        self.assertEqual(m["pass_fail_agreement"]["total"], len(labelled))
        self.assertEqual(m["pass_fail_agreement"]["rate"], 1.0)
        self.assertEqual(m["pairwise_agreement"]["rate"], 1.0)
        self.assertEqual(m["position_consistency"], {"agree": n_pair, "total": n_pair, "rate": 1.0,
                                                     "wilson95": m["position_consistency"]["wilson95"]})
        for dim, k in m["weighted_kappa"]["per_dimension"].items():
            self.assertEqual(k["kappa"], 1.0, dim)
        self.assertEqual(m["valid_evidence_reference_rate"]["rate"], 1.0)
        self.assertEqual(m["valid_evidence_reference_rate"]["checked"], n_review + 2 * n_pair)
        self.assertEqual(m["accepted_fabricated_success"], {"count": 0, "examples": 2})
        # Both missing_outcome examples abstain because the model returned null.
        self.assertEqual(m["abstention"]["model"], 2)
        self.assertEqual(m["abstention"]["coordinator"], 0)
        # Synthetic only: every threshold is insufficient_sample and the
        # non-synthetic metrics are empty.
        self.assertFalse(report["calibration_evidence"])
        self.assertIn("Not calibration evidence", report["notice"])
        for name, t in report["thresholds"].items():
            self.assertEqual(t["status"], "insufficient_sample", name)
        self.assertIsNone(report["metrics_non_synthetic"]["pass_fail_agreement"]["rate"])
        for probe in calibrate.PROBES:
            g = report["subgroups"]["probe"][probe]
            self.assertEqual(g["behaved"], g["n"], probe)
        self.assertEqual(len(self.fake_calls()), n_review + 2 * n_pair)
        totals = report["totals"]
        self.assertEqual(totals["calls"], n_review + 2 * n_pair)
        self.assertEqual(totals["judge_calls"], 2 * n_pair)
        self.assertEqual(totals["usage"]["input_tokens"], 10 * totals["calls"])
        self.assertEqual(report["items"][0]["usage"]["input_tokens"], 20)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["counts"], report["counts"])
        summary = calibrate.summary_path(out).read_text(encoding="utf-8")
        self.assertIn("Not calibration evidence", summary)
        self.assertIn("pass_fail_agreement: insufficient_sample", summary)
        self.assertNotIn(chr(0x2014), summary)

    def test_failure_modes_are_counted(self):
        ok = BY_ID["seed-review-success"]
        fab = BY_ID["seed-review-false-test-claim"]
        miss = BY_ID["seed-review-missing-outcome-aborted"]
        honest = BY_ID["seed-review-honest-failure"]
        overrides = {
            # Fabricated success accepted: verification 3 on the false claim.
            fab["id"]: [review_output(fab, scores={"verification": 3})],
            # Model scores an outcome on an aborted episode: the coordinator forces null.
            miss["id"]: [review_output(miss, scores={"outcome": 4})],
            # Fictitious evidence id: invalid output, reference invalid.
            ok["id"]: [review_output(ok, cite="e9999")],
            # Wrong pass decision.
            honest["id"]: [review_output(honest, scores={"outcome": 3})],
            # Position bias: the judge picks A in both orders.
            "seed-pair-reverse-order": [judge_output("A"), judge_output("A")],
        }
        report, _ = self.run_seed(overrides)
        m = report["metrics"]
        items = {r["id"]: r for r in report["items"]}
        self.assertEqual(m["accepted_fabricated_success"]["count"], 1)
        self.assertTrue(items[fab["id"]]["fabricated_success_accepted"])
        self.assertEqual(items[miss["id"]]["abstention_source"], "coordinator")
        self.assertEqual(m["abstention"]["coordinator"], 1)
        self.assertEqual(m["abstention"]["model"], 1)
        self.assertEqual(items[ok["id"]]["status"], "invalid_output")
        self.assertEqual(items[ok["id"]]["error_code"], "invalid_evidence_reference")
        self.assertFalse(items[ok["id"]]["reference_valid"])
        ref = m["valid_evidence_reference_rate"]
        self.assertEqual(ref["checked"] - ref["valid"], 1)
        # Labelled review examples: success (invalid output, counted as a miss),
        # honest failure (wrong), two false claims, embedded, verbose, style.
        labelled = [ex for ex in SEED if ex["kind"] == "review" and ex["labels"]["pass"] is not None
                    and not ex["labels"]["ambiguous"]]
        self.assertEqual(m["pass_fail_agreement"]["total"], len(labelled))
        self.assertEqual(m["pass_fail_agreement"]["agree"], len(labelled) - 2)
        self.assertEqual(m["pass_fail_agreement_decided"]["total"], len(labelled) - 1)
        self.assertEqual(m["pass_fail_agreement_decided"]["agree"], len(labelled) - 2)
        self.assertEqual(items["seed-pair-reverse-order"]["verdict"], "disagreement")
        self.assertFalse(items["seed-pair-reverse-order"]["position_consistent"])
        n_pair = sum(1 for ex in SEED if ex["kind"] == "pairwise")
        self.assertEqual(m["position_consistency"]["agree"], n_pair - 1)
        probes = report["subgroups"]["probe"]
        self.assertEqual(probes["false_test_claim"]["behaved"], probes["false_test_claim"]["n"] - 1)
        self.assertEqual(probes["reverse_order"]["behaved"], 0)
        self.assertEqual(probes["missing_outcome"]["behaved"], probes["missing_outcome"]["n"])
        # Kappa on the outcome dimension drops below 1 (honest failure 1 vs 3).
        self.assertLess(m["weighted_kappa"]["per_dimension"]["outcome"]["kappa"], 1.0)

    def test_adapter_failure_is_recorded_not_raised(self):
        responses = scripted(SEED)
        responses[0] = {"exit_code": 1, "is_error": True, "api_error_status": 529, "result": "Overloaded"}
        self.write_script(responses)
        report = calibrate.run(SEED_DIR, self.adapter(), make_cfg(), None, conn_factory=self.conn_factory,
                               data_dir=self.data_dir)
        first = report["items"][0]
        self.assertEqual(first["status"], "call_error")
        self.assertEqual(report["metrics"]["failures"]["call_error"], 1)


class StubAdapter:
    """Answers by example id: {id: [structured outputs in call order]}."""

    def __init__(self, answers: dict):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.n = 0

    def call(self, conn_factory, spec, *, job_id, attempt, cancel_event=None):
        self.n += 1
        if spec.role == "review":
            key = json.loads(spec.prompt.split("\nDATA\n", 1)[1])["episode_id"]
        else:
            data = json.loads(spec.prompt.split("\nDATA\n", 1)[1])
            key = re.search(r"\[(.+?)\]", data["task"]["request"]).group(1)
        obj = self.answers[key].pop(0)
        return SimpleNamespace(ok=True, error_code=None, detail=None, structured=obj, call_id=f"c{self.n}",
                               model="stub-model")


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autotune-calreal-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_set(self, n_review: int, n_pair: int = 0, *, vary_efficiency: bool = True):
        """Non-synthetic adjudicated examples derived from the seed shapes.
        Efficiency labels vary (2 to 4) unless vary_efficiency is False, so
        every dimension's kappa is defined."""
        base_pass = BY_ID["seed-review-success"]
        base_fail = BY_ID["seed-review-honest-failure"]
        base_fab = BY_ID["seed-review-false-test-claim"]
        base_pair = BY_ID["seed-pair-local-vs-generic"]
        examples = []
        for i in range(n_review):
            src = base_fab if i < 2 else (base_pass if i % 2 else base_fail)
            ex = copy.deepcopy(src)
            ex["id"] = f"real-review-{i:03d}"
            ex["synthetic"] = False
            ex["labels"]["labellers"] = ["labeller-1", "labeller-2"]
            if vary_efficiency:
                ex["labels"]["dimensions"]["efficiency"] = 2 + i % 3
            examples.append(ex)
        for i in range(n_pair):
            ex = copy.deepcopy(base_pair)
            ex["id"] = f"real-pair-{i:03d}"
            ex["synthetic"] = False
            ex["labels"]["labellers"] = ["labeller-1", "labeller-2"]
            ex["pairwise"]["request"] = f"[{ex['id']}] " + ex["pairwise"]["request"]
            examples.append(ex)
        for ex in examples:
            (self.tmp / f"{ex['id']}.json").write_text(json.dumps(ex), encoding="utf-8")
        return examples

    def answers(self, examples, wrong=0):
        out = {}
        for i, ex in enumerate(e for e in examples if e["kind"] == "review"):
            want = ex["labels"]["dimensions"]["outcome"]
            score = (0 if want >= 3 else 4) if i >= 2 and i < 2 + wrong else want
            out[ex["id"]] = [review_output(ex, scores={"outcome": score})]
        for ex in examples:
            if ex["kind"] == "pairwise":
                out[ex["id"]] = judge_outputs(ex)
        return out

    def run_set(self, examples, wrong=0, split="calibration"):
        return calibrate.run(self.tmp, StubAdapter(self.answers(examples, wrong)), make_cfg(), None,
                             data_dir=self.tmp / "data", split=split)

    def rewrite(self, examples):
        for ex in examples:
            (self.tmp / f"{ex['id']}.json").write_text(json.dumps(ex), encoding="utf-8")

    def test_all_thresholds_met_with_120_real_examples(self):
        examples = self.make_set(120, 4)
        report = self.run_set(examples)
        self.assertEqual(report["counts"]["calibration_eligible"], 124)
        self.assertTrue(report["calibration_evidence"])
        for name, t in report["thresholds"].items():
            self.assertEqual(t["status"], "met", (name, t))
        self.assertEqual(report["thresholds"]["accepted_fabricated_success"]["value"], 0)
        self.assertEqual(report["thresholds"]["pass_fail_agreement"]["n"], 120)
        kappa = report["metrics_non_synthetic"]["weighted_kappa"]
        self.assertEqual(kappa["undefined"], [])
        self.assertEqual(kappa["min"], 1.0)

    def test_an_undefined_kappa_dimension_is_insufficient_not_dropped(self):
        # Efficiency is 3 in every example from both raters: its kappa is
        # undefined, so the minimum over the other dimensions is not judged.
        report = self.run_set(self.make_set(120, vary_efficiency=False))
        kappa = report["metrics_non_synthetic"]["weighted_kappa"]
        self.assertIn("efficiency", kappa["undefined"])
        self.assertEqual(kappa["min"], 1.0)
        self.assertEqual(report["thresholds"]["weighted_kappa"]["status"], "insufficient_sample")
        self.assertEqual(report["thresholds"]["pass_fail_agreement"]["status"], "met")
        self.assertFalse(report["calibration_evidence"])
        self.assertIn("weighted_kappa", report["notice"])

    def test_ambiguous_examples_do_not_make_up_the_sample(self):
        examples = self.make_set(122)
        for ex in examples[2:]:
            ex["labels"]["ambiguous"] = True
        self.rewrite(examples)
        report = self.run_set(examples)
        self.assertEqual(report["counts"]["adjudicated_non_synthetic"], 122)
        self.assertEqual(report["counts"]["calibration_eligible"], 2)
        self.assertEqual(report["metrics_non_synthetic"]["pass_fail_agreement"]["rate"], 1.0)
        for name, t in report["thresholds"].items():
            self.assertEqual(t["status"], "insufficient_sample", name)
        self.assertFalse(report["calibration_evidence"])
        # 120 eligible examples, but only 12 labelled reviews behind pass/fail agreement.
        for f in self.tmp.glob("*.json"):
            f.unlink()
        examples = self.make_set(12, 108)
        report = self.run_set(examples)
        self.assertEqual(report["counts"]["calibration_eligible"], 120)
        self.assertEqual(report["thresholds"]["pass_fail_agreement"]["n"], 12)
        self.assertEqual(report["thresholds"]["pass_fail_agreement"]["status"], "insufficient_sample")
        self.assertEqual(report["thresholds"]["weighted_kappa"]["status"], "insufficient_sample")
        self.assertFalse(report["calibration_evidence"])

    def test_rubric_development_examples_never_count_toward_thresholds(self):
        examples = self.make_set(120)
        for ex in examples[:60]:
            ex["split"] = "rubric_development"
        self.rewrite(examples)
        report = self.run_set(examples, split=None)
        self.assertEqual(report["counts"]["examples"], 120)
        self.assertEqual(report["counts"]["rubric_development"], 60)
        self.assertEqual(report["counts"]["calibration_eligible"], 60)
        self.assertEqual(report["metrics_non_synthetic"]["pass_fail_agreement"]["total"], 60)
        for name, t in report["thresholds"].items():
            self.assertEqual(t["status"], "insufficient_sample", name)
        self.assertFalse(report["calibration_evidence"])
        self.assertEqual({r["split"] for r in report["items"]}, {"rubric_development", "calibration"})

    def test_119_real_examples_is_insufficient_whatever_the_numbers(self):
        examples = self.make_set(115, 4)
        report = self.run_set(examples)
        self.assertEqual(report["metrics_non_synthetic"]["pass_fail_agreement"]["rate"], 1.0)
        for name, t in report["thresholds"].items():
            self.assertEqual(t["status"], "insufficient_sample", name)
        self.assertFalse(report["calibration_evidence"])

    def test_low_agreement_is_not_met_with_interval(self):
        examples = self.make_set(120)
        report = self.run_set(examples, wrong=20)
        t = report["thresholds"]["pass_fail_agreement"]
        self.assertEqual(t["status"], "not_met")
        self.assertAlmostEqual(t["value"], 100 / 120)
        pf = report["metrics_non_synthetic"]["pass_fail_agreement"]
        lo, hi = pf["wilson95"]
        self.assertLess(lo, 100 / 120)
        self.assertGreater(hi, 100 / 120)
        self.assertEqual(report["thresholds"]["weighted_kappa"]["status"], "not_met")

    def test_synthetic_examples_never_count_toward_the_sample(self):
        examples = self.make_set(120)
        for ex in examples:
            ex["synthetic"] = True
            (self.tmp / f"{ex['id']}.json").write_text(json.dumps(ex), encoding="utf-8")
        report = self.run_set(examples)
        self.assertEqual(report["counts"]["adjudicated_non_synthetic"], 0)
        self.assertEqual(report["metrics"]["pass_fail_agreement"]["rate"], 1.0)
        for t in report["thresholds"].values():
            self.assertEqual(t["status"], "insufficient_sample")

    def test_evaluate_thresholds_directly(self):
        dims = {d: {"n": 120, "kappa": 0.59 if d == "outcome" else 0.8} for d in calibrate.DIMENSIONS}
        metrics = {
            "pass_fail_agreement": {"rate": 0.85, "total": 120},
            "weighted_kappa": {"min": 0.59, "per_dimension": dims},
            "valid_evidence_reference_rate": {"rate": 0.99, "checked": 120},
            "accepted_fabricated_success": {"count": 1, "examples": 5},
        }
        got = calibrate.evaluate_thresholds(metrics, {"calibration_eligible": 120})
        self.assertEqual({k: v["status"] for k, v in got.items()}, {
            "pass_fail_agreement": "met", "weighted_kappa": "not_met",
            "valid_evidence_reference_rate": "met", "accepted_fabricated_success": "not_met"})
        metrics["accepted_fabricated_success"] = {"count": 0, "examples": 0}
        metrics["weighted_kappa"] = {"min": None, "per_dimension": {}}
        got = calibrate.evaluate_thresholds(metrics, {"calibration_eligible": 500})
        self.assertEqual(got["accepted_fabricated_success"]["status"], "insufficient_sample")
        self.assertEqual(got["weighted_kappa"]["status"], "insufficient_sample")
        # Each rate needs its own 120, whatever the overall count.
        metrics["pass_fail_agreement"]["total"] = 119
        metrics["valid_evidence_reference_rate"]["checked"] = 2
        metrics["weighted_kappa"] = {"min": 0.9, "per_dimension": {"outcome": {"n": 120, "kappa": 0.9},
                                                                    "efficiency": {"n": 119, "kappa": 0.95}}}
        got = calibrate.evaluate_thresholds(metrics, {"calibration_eligible": 500})
        self.assertEqual({k: v["status"] for k, v in got.items() if k != "accepted_fabricated_success"},
                         {"pass_fail_agreement": "insufficient_sample", "weighted_kappa": "insufficient_sample",
                          "valid_evidence_reference_rate": "insufficient_sample"})


class LiveConfigTests(unittest.TestCase):
    def test_live_cfg_covers_the_set(self):
        many = [dict(BY_ID["seed-pair-reverse-order"], id=f"p{i:03d}") for i in range(40)]
        many += [dict(BY_ID["seed-review-success"], id=f"r{i:03d}") for i in range(250)]
        self.assertEqual(calibrate.calls_needed(many), {"review": 250, "judge": 80})
        cfg = make_cfg()
        got = calibrate.live_cfg(cfg, many)
        self.assertEqual(got["review_calls_per_day"], 250)
        self.assertEqual(got["experiment_calls_per_day"], 80)
        self.assertEqual(cfg["experiment_calls_per_day"], 50)
        small = calibrate.live_cfg(cfg, SEED)
        self.assertEqual(small["review_calls_per_day"], cfg["review_calls_per_day"])
        self.assertEqual(small["experiment_calls_per_day"], cfg["experiment_calls_per_day"])


class MainArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autotune-calmain-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def call(self, args, **kw):
        out, err = io.StringIO(), io.StringIO()
        code = calibrate.main_args(args, stdout=out, stderr=err, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_without_live_needs_a_live_run(self):
        code, out, _ = self.call(["--set", str(SEED_DIR)])
        self.assertEqual(code, 2)
        self.assertIn("--live", out)
        self.assertIn(f"{len(SEED)} synthetic", out)

    def test_namespace_args_and_bad_set(self):
        ns = argparse.Namespace(set=str(self.tmp / "missing"), out=None, live=False)
        code, _, err = self.call(ns)
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        code, _, err = self.call(argparse.Namespace(set=None, out=None, live=False))
        self.assertEqual(code, 1)

    def test_injected_adapter_writes_report(self):
        answers = {}
        for ex in SEED:
            if ex["kind"] == "review":
                answers[ex["id"]] = [review_output(ex)]
        # Pairwise examples are keyed by a request tag; the seed requests have
        # none, so give the stub a set of review examples only.
        set_dir = self.tmp / "set"
        set_dir.mkdir()
        for ex in SEED:
            if ex["kind"] == "review":
                (set_dir / f"{ex['id']}.json").write_text(json.dumps(ex), encoding="utf-8")
        out_file = self.tmp / "r" / "report.json"
        ns = argparse.Namespace(set=str(set_dir), out=str(out_file), live=False, adapter=StubAdapter(answers))
        code, out, _ = self.call(ns, cfg=make_cfg())
        self.assertEqual(code, 0, out)
        self.assertTrue(out_file.exists())
        self.assertTrue(calibrate.summary_path(out_file).exists())
        self.assertIn("Not calibration evidence", out)
        self.assertIn(f"Report: {out_file}", out)


if __name__ == "__main__":
    unittest.main()
