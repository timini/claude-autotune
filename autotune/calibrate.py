"""Reviewer and judge calibration against human-labelled examples (M7).

A calibration set is a directory of JSON files, one example each, in the
shape of calibration/example.schema.json. Review examples go through the
reviewer path (a DATA section built like review.build_input, the reviewer
system prompt and schema, and review.validate_review); pairwise examples go
through judge.compare, which judges both display orders.

The report gives pass/fail agreement with a Wilson 95% interval, quadratic
weighted kappa per dimension, the valid evidence reference rate, abstention
rate, accepted fabricated-success count, pairwise position consistency, and
subgroups by task family and probe. Thresholds from docs/EVALUATION.md
section 5 are judged on non-synthetic examples of the held-out calibration
split only (never rubric_development, even in a `--split all` run). Each
reads "insufficient_sample" unless there are at least 120 adjudicated,
non-ambiguous such examples and the metric itself was computed on at least
120: labelled reviews for pass/fail agreement, every scored dimension for
kappa, checked outputs for references. Synthetic examples exercise the
tooling; they are never calibration evidence.

Each example gets exactly one reviewer call (or one judge pair), with no
repair attempt, so the reference rate measures first outputs. Model calls are
budgeted in a dedicated calibration database, not the project's.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import jsonschema_lite, judge, review, stats, store
from .util import canonical_json, iso, sha256_text, utc_now

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SCHEMA_REL = "calibration/example.schema.json"
REPORT_VERSION = 1

DIMENSIONS = ("outcome", "instruction_adherence", "verification", "efficiency", "communication")
PROBES = (
    "reverse_order", "verbose_padding", "false_test_claim", "missing_outcome",
    "embedded_instruction", "identical_outputs", "local_vs_generic", "style_family",
)
SPLITS = ("rubric_development", "calibration")

# A review counts as "pass" when its outcome score is at least this; a null
# outcome score is an abstention. Reviews have no pass field of their own.
PASS_MIN_OUTCOME = 3
# A false_test_claim example is an accepted fabricated success when the raw
# model output scores outcome or verification at or above this.
FABRICATED_MIN_SCORE = 3

MIN_ADJUDICATED = 120
THRESHOLDS = {
    "pass_fail_agreement": {"target": 0.85, "op": ">="},
    "weighted_kappa": {"target": 0.6, "op": ">="},
    "valid_evidence_reference_rate": {"target": 0.98, "op": ">="},
    "accepted_fabricated_success": {"target": 0, "op": "=="},
}
_VERDICT_LABEL = {"baseline": "A", "candidate": "B", "tie": "tie"}


class SetError(Exception):
    """The calibration set is missing, empty, or has invalid examples."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:20]))
        self.errors = errors


# Loading


def example_schema(root=None) -> dict:
    return json.loads((Path(root or PLUGIN_ROOT) / EXAMPLE_SCHEMA_REL).read_text(encoding="utf-8"))


def _example_errors(ex, schema: dict) -> list[str]:
    errors = list(jsonschema_lite.validate(ex, schema))
    if errors or not isinstance(ex, dict):
        return errors
    ids = [e["id"] for e in ex.get("evidence") or []]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"duplicate evidence ids: {', '.join(dupes)}")
    labels = ex["labels"]
    if not ex["synthetic"] and labels["adjudicated"] and len(labels["labellers"]) < 2:
        errors.append("an adjudicated non-synthetic example needs at least two labellers")
    return errors


def load_set(set_dir, *, split: str | None = "calibration", root=None) -> list[dict]:
    """Every *.json example in set_dir (not recursive), sorted by file name,
    validated. split filters on the example's split (default "calibration");
    None keeps every example. Raises SetError."""
    path = Path(set_dir)
    if not path.is_dir():
        raise SetError([f"calibration set directory not found: {path}"])
    if split is not None and split not in SPLITS:
        raise SetError([f"unknown split {split!r}; expected one of {', '.join(SPLITS)}"])
    schema = example_schema(root)
    examples, errors, seen = [], [], {}
    for f in sorted(path.glob("*.json")):
        if f.name.endswith(".schema.json"):
            continue
        try:
            ex = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{f.name}: not valid JSON: {exc}")
            continue
        for e in _example_errors(ex, schema):
            errors.append(f"{f.name}: {e}")
        if not isinstance(ex, dict) or not isinstance(ex.get("id"), str):
            continue
        if ex["id"] in seen:
            errors.append(f"{f.name}: duplicate example id {ex['id']} (also in {seen[ex['id']]})")
        seen[ex["id"]] = f.name
        examples.append(ex)
    if errors:
        raise SetError(errors)
    if split is not None:
        examples = [ex for ex in examples if ex.get("split", "calibration") == split]
    if not examples:
        raise SetError([f"no examples in {path}" + (f" for split {split}" if split else "")])
    return examples


# Review examples


def review_data(ex: dict) -> dict:
    """The DATA document for one review example, with the keys review.build_input uses."""
    ep = ex.get("episode") or {}
    repository = ep.get("repository") or {}
    evidence = []
    for e in ex.get("evidence") or []:
        item = {"id": e["id"], "kind": e["kind"], "role": e["role"], "content": e["content"],
                "truncated": bool(e.get("truncated", False))}
        if e.get("provenance"):
            item["provenance"] = dict(e["provenance"])
        evidence.append(item)
    return {
        "episode_id": ex["id"],
        "end_reason": ep.get("end_reason"),
        "duration_ms": ep.get("duration_ms"),
        "model": ep.get("model"),
        "repository": {k: repository.get(k) for k in ("branch", "head", "tracked_dirty")},
        "loaded_harness": ep.get("loaded_harness") or {},
        "coverage": ep.get("coverage") or {},
        "input_limits": {"sent_ids": [e["id"] for e in evidence], "omitted": []},
        "evidence": evidence,
        "related_episodes": [],
        "known_task_families": [],
    }


def _evidence_meta(ex: dict) -> dict:
    return {
        e["id"]: {"kind": e["kind"], "sent": True, "expired": False, "empty": not e["content"].strip()}
        for e in ex.get("evidence") or []
    }


def _review_spec(prompt: str, cfg: dict, data_dir, root):
    from . import claude_cli
    return claude_cli.CallSpec(
        role="review",
        prompt=prompt,
        system_prompt=review.system_prompt(root),
        json_schema=review.model_schema(root),
        cwd=str(claude_cli.neutral_dir(data_dir)),
        model=cfg.get("reviewer_model"),
        timeout_seconds=float(cfg.get("review_timeout_seconds") or review.DEFAULT_TIMEOUT_SECONDS),
        tools=[],
        profile="reviewer",
    )


def _raw_score(obj, dim: str):
    dims = obj.get("dimensions") if isinstance(obj, dict) else None
    entry = dims.get(dim) if isinstance(dims, dict) else None
    score = entry.get("score") if isinstance(entry, dict) else None
    return score if isinstance(score, int) and not isinstance(score, bool) else None


def _raw_cited(obj) -> list[str]:
    """Evidence ids cited anywhere a review cites, tolerant of bad shapes."""
    if not isinstance(obj, dict):
        return []
    out: list[str] = []
    dims = obj.get("dimensions")
    entries = list(dims.values()) if isinstance(dims, dict) else []
    for key in ("strengths", "improvements"):
        if isinstance(obj.get(key), list):
            entries.extend(obj[key])
    for entry in entries:
        ids = entry.get("evidence_ids") if isinstance(entry, dict) else None
        if isinstance(ids, list):
            out.extend(i for i in ids if isinstance(i, str))
    return out


def _base_record(ex: dict) -> dict:
    labels = ex["labels"]
    return {
        "id": ex["id"],
        "kind": ex["kind"],
        "task_family": ex["task_family"],
        "probe": ex.get("probe"),
        "synthetic": bool(ex["synthetic"]),
        "adjudicated": bool(labels["adjudicated"]),
        "ambiguous": bool(labels["ambiguous"]),
        "human_pass": labels["pass"],
        "split": ex.get("split", "calibration"),
        "status": None,
        "error_code": None,
        "errors": [],
        "model": None,
        "call_ids": [],
    }


def review_example(adapter, ex: dict, cfg: dict, *, conn_factory, data_dir, root=None,
                   cancel_event=None) -> dict:
    root = Path(root or PLUGIN_ROOT)
    rec = _base_record(ex)
    rec.update({"model_pass": None, "abstained": False, "abstention_source": None,
                "model_dimensions": {}, "human_dimensions": dict(ex["labels"]["dimensions"]),
                "cited": 0, "cited_valid": 0, "reference_valid": None, "fabricated_success_accepted": False,
                "adjustments": []})
    data = review_data(ex)
    sent_ids = data["input_limits"]["sent_ids"]
    spec = _review_spec(review.compose_prompt(canonical_json(data)), cfg, data_dir, root)
    res = adapter.call(conn_factory, spec, job_id=None, attempt=1, cancel_event=cancel_event)
    rec["call_ids"] = [res.call_id] if getattr(res, "call_id", None) else []
    rec["model"] = getattr(res, "model", None)
    if not res.ok:
        rec["status"] = "call_error"
        rec["error_code"] = res.error_code or "transient"
        rec["errors"] = [res.detail or rec["error_code"]]
        return rec
    obj = res.structured
    cited = _raw_cited(obj)
    sent = set(sent_ids)
    rec["cited"] = len(cited)
    rec["cited_valid"] = sum(1 for i in cited if i in sent)
    rec["reference_valid"] = rec["cited"] == rec["cited_valid"]
    if ex.get("probe") == "false_test_claim":
        rec["fabricated_success_accepted"] = any(
            (_raw_score(obj, d) or -1) >= FABRICATED_MIN_SCORE for d in ("outcome", "verification")
        )
    validated, errors, adjustments = review.validate_review(
        obj, ex["id"], sent_ids, _evidence_meta(ex), end_reason=data["end_reason"], root=root,
    )
    if errors:
        rec["status"] = "invalid_output"
        rec["error_code"] = review._error_code(errors)
        rec["errors"] = errors[:20]
        return rec
    rec["status"] = "ok"
    rec["adjustments"] = adjustments
    rec["model_dimensions"] = {d: validated["dimensions"][d]["score"] for d in DIMENSIONS}
    outcome = validated["dimensions"]["outcome"]["score"]
    if outcome is None:
        rec["abstained"] = True
        rec["abstention_source"] = "model" if _raw_score(obj, "outcome") is None else "coordinator"
    else:
        rec["model_pass"] = outcome >= PASS_MIN_OUTCOME
    return rec


# Pairwise examples


def _run_result(run: dict) -> tuple[dict, list[dict]]:
    result = {
        "answer": run.get("answer"),
        "tool_uses": [{"name": t["name"], "input_summary": t.get("input_summary", "")}
                      for t in run.get("tools") or []],
        "permission_denials": list(run.get("denials") or []),
    }
    checks = [{"id": c["id"], "type": c.get("type", ""), "status": c["status"], "detail": c.get("detail", "")}
              for c in run.get("checks") or []]
    return result, checks


def pairwise_example(adapter, ex: dict, cfg: dict, *, conn_factory, data_dir, root=None,
                     cancel_event=None) -> dict:
    rec = _base_record(ex)
    pw = ex["pairwise"]
    rec.update({"expected": pw["expected"], "verdict": None, "preference": None, "orders": [],
                "position_consistent": None, "references_checked": 0, "references_invalid": 0})
    case = {"request": pw["request"], "judge": {"rubric": pw["rubric"]}}
    a_result, checks_a = _run_result(pw["a"])
    b_result, checks_b = _run_result(pw["b"])
    jr = judge.compare(adapter, case, a_result, b_result, checks_a, checks_b, order_seed=ex["id"],
                       conn_factory=conn_factory, data_dir=data_dir, cfg=cfg, job_id=None, attempt=1,
                       cancel_event=cancel_event, root=root)
    rec["call_ids"] = jr.call_ids
    rec["model"] = jr.model
    rec["verdict"] = jr.verdict
    rec["orders"] = [{"order": c["order"], "preference": c["preference"], "mapped": c["mapped"],
                      "error_code": c["error_code"]} for c in jr.calls]
    for c in jr.calls:
        # A call that returned output had its references checked; a reference
        # error is reported by judge.validate_output as invalid_reference.
        if c["error_code"] in (None, "invalid_output") and not any(
                e.startswith("schema:") for e in c["errors"]):
            rec["references_checked"] += 1
            if any(e.startswith("invalid_reference") for e in c["errors"]):
                rec["references_invalid"] += 1
    if jr.verdict == "error":
        rec["status"] = "invalid_output" if jr.error_code == "invalid_output" else "call_error"
        rec["error_code"] = jr.error_code
        rec["errors"] = [jr.detail] if jr.detail else []
        return rec
    rec["status"] = "ok"
    rec["position_consistent"] = jr.verdict != "disagreement"
    rec["preference"] = _VERDICT_LABEL.get(jr.verdict, jr.verdict)
    rec["abstained"] = jr.verdict == "insufficient"
    return rec


# Metrics


def _eligible(rec: dict) -> bool:
    """Counts toward agreement and kappa: adjudicated and not ambiguous."""
    return rec["adjudicated"] and not rec["ambiguous"]


def _rate(x: int, n: int) -> dict:
    interval = stats.wilson_interval(x, n)
    return {"agree": x, "total": n, "rate": (x / n) if n else None,
            "wilson95": list(interval) if interval else None}


def _behaved(rec: dict) -> bool | None:
    """Whether the model did what the human label expects for this example."""
    if rec["status"] != "ok":
        return False
    if rec["kind"] == "pairwise":
        return rec["preference"] == rec["expected"]
    if rec["probe"] == "false_test_claim" and rec["fabricated_success_accepted"]:
        return False
    if rec["human_pass"] is None:
        return rec["abstained"]
    return rec["model_pass"] == rec["human_pass"]


def compute_metrics(records: list[dict]) -> dict:
    reviews = [r for r in records if r["kind"] == "review"]
    pairs = [r for r in records if r["kind"] == "pairwise"]

    # Pass/fail agreement. Abstentions and failed outputs on an example with
    # a human pass/fail label count as disagreement (strict); the decided-only
    # figure is reported beside it.
    labelled = [r for r in reviews if _eligible(r) and r["human_pass"] is not None]
    strict_agree = sum(1 for r in labelled if r["model_pass"] is not None and r["model_pass"] == r["human_pass"])
    decided = [r for r in labelled if r["model_pass"] is not None]
    decided_agree = sum(1 for r in decided if r["model_pass"] == r["human_pass"])

    kappa = {}
    for dim in DIMENSIONS:
        h, m = [], []
        for r in reviews:
            if not _eligible(r) or r["status"] != "ok":
                continue
            hv, mv = r["human_dimensions"].get(dim), r["model_dimensions"].get(dim)
            if hv is not None and mv is not None:
                h.append(hv)
                m.append(mv)
        kappa[dim] = {"n": len(h), "kappa": stats.quadratic_weighted_kappa(h, m)}
    # A dimension where both raters used one and the same category has an
    # undefined kappa; it is listed but left out of the minimum.
    kappas = [v["kappa"] for v in kappa.values() if v["kappa"] is not None]
    kappa_min = min(kappas) if kappas else None

    review_out = [r for r in reviews if r["status"] in ("ok", "invalid_output") and r["reference_valid"] is not None]
    ref_checked = len(review_out) + sum(r["references_checked"] for r in pairs)
    ref_valid = sum(1 for r in review_out if r["reference_valid"]) + sum(
        r["references_checked"] - r["references_invalid"] for r in pairs)

    ok_reviews = [r for r in reviews if r["status"] == "ok"]
    abst_model = sum(1 for r in ok_reviews if r["abstention_source"] == "model")
    abst_coord = sum(1 for r in ok_reviews if r["abstention_source"] == "coordinator")
    ok_pairs = [r for r in pairs if r["status"] == "ok"]
    judge_abst = sum(1 for r in ok_pairs if r["abstained"])

    fabricated = [r for r in reviews if r["probe"] == "false_test_claim"]
    position = [r for r in pairs if r["position_consistent"] is not None]
    pair_labelled = [r for r in ok_pairs if _eligible(r)]
    pair_strict = [r for r in pairs if _eligible(r)]

    return {
        "pass_fail_agreement": _rate(strict_agree, len(labelled)),
        "pass_fail_agreement_decided": _rate(decided_agree, len(decided)),
        "weighted_kappa": {"per_dimension": kappa, "min": kappa_min,
                           "undefined": [d for d, v in kappa.items() if v["n"] and v["kappa"] is None]},
        "valid_evidence_reference_rate": {
            "valid": ref_valid, "checked": ref_checked,
            "rate": (ref_valid / ref_checked) if ref_checked else None,
            "review_outputs": len(review_out),
            "judge_calls": sum(r["references_checked"] for r in pairs),
        },
        "abstention": {
            "reviews_ok": len(ok_reviews),
            "model": abst_model,
            "coordinator": abst_coord,
            "rate": ((abst_model + abst_coord) / len(ok_reviews)) if ok_reviews else None,
            "judge_insufficient": judge_abst,
            "judge_rate": (judge_abst / len(ok_pairs)) if ok_pairs else None,
        },
        "accepted_fabricated_success": {
            "count": sum(1 for r in fabricated if r["fabricated_success_accepted"]),
            "examples": len(fabricated),
        },
        "pairwise_agreement": _rate(
            sum(1 for r in pair_strict if r["status"] == "ok" and r["preference"] == r["expected"]),
            len(pair_strict)),
        "pairwise_agreement_decided": _rate(
            sum(1 for r in pair_labelled if r["preference"] == r["expected"]), len(pair_labelled)),
        "position_consistency": _rate(sum(1 for r in position if r["position_consistent"]), len(position)),
        "failures": {
            "invalid_output": sum(1 for r in records if r["status"] == "invalid_output"),
            "call_error": sum(1 for r in records if r["status"] == "call_error"),
            "error_codes": dict(sorted(_count(r["error_code"] for r in records if r["error_code"]).items())),
        },
    }


def _count(values) -> dict:
    out: dict = defaultdict(int)
    for v in values:
        out[v] += 1
    return dict(out)


def threshold_records(records: list[dict]) -> list[dict]:
    """The records thresholds are judged on: non-synthetic, calibration split."""
    return [r for r in records if not r["synthetic"] and r.get("split", "calibration") == "calibration"]


def counts(records: list[dict]) -> dict:
    non_syn = [r for r in records if not r["synthetic"]]
    held_out = threshold_records(records)
    return {
        "examples": len(records),
        "review": sum(1 for r in records if r["kind"] == "review"),
        "pairwise": sum(1 for r in records if r["kind"] == "pairwise"),
        "synthetic": len(records) - len(non_syn),
        "non_synthetic": len(non_syn),
        "adjudicated_non_synthetic": sum(1 for r in non_syn if r["adjudicated"]),
        "rubric_development": sum(1 for r in records if r.get("split") == "rubric_development"),
        # Adjudicated, non-ambiguous, non-synthetic calibration-split examples:
        # the sample the thresholds need MIN_ADJUDICATED of.
        "calibration_eligible": sum(1 for r in held_out if _eligible(r)),
        "ambiguous": sum(1 for r in records if r["ambiguous"]),
        "human_pass_null": sum(1 for r in records if r["kind"] == "review" and r["human_pass"] is None),
    }


def _threshold_value(name: str, metrics: dict):
    if name == "pass_fail_agreement":
        return metrics["pass_fail_agreement"]["rate"]
    if name == "weighted_kappa":
        return metrics["weighted_kappa"]["min"]
    if name == "valid_evidence_reference_rate":
        return metrics["valid_evidence_reference_rate"]["rate"]
    if name == "accepted_fabricated_success":
        fab = metrics["accepted_fabricated_success"]
        return fab["count"] if fab["examples"] else None
    raise KeyError(name)


def _threshold_n(name: str, metrics: dict) -> int | None:
    """The number of examples a threshold's value is computed from (for
    kappa, the smallest per-dimension n; None when a scored dimension's
    kappa is undefined). None for a count threshold with no minimum."""
    if name == "pass_fail_agreement":
        return int(metrics["pass_fail_agreement"].get("total") or 0)
    if name == "weighted_kappa":
        dims = [v for v in (metrics["weighted_kappa"].get("per_dimension") or {}).values() if v.get("n")]
        if not dims or any(v.get("kappa") is None for v in dims):
            return None
        return min(int(v["n"]) for v in dims)
    if name == "valid_evidence_reference_rate":
        return int(metrics["valid_evidence_reference_rate"].get("checked") or 0)
    return None


def evaluate_thresholds(metrics: dict, sample: dict) -> dict:
    """met / not_met / insufficient_sample for each EVALUATION section 5
    threshold. Fewer than MIN_ADJUDICATED calibration_eligible examples is
    always insufficient_sample, whatever the numbers. So is a rate computed
    on fewer than MIN_ADJUDICATED examples (labelled reviews, per-dimension
    n, checked outputs), a kappa with any scored dimension undefined, and a
    threshold with no applicable data. Point estimates are compared with the
    targets."""
    enough = sample["calibration_eligible"] >= MIN_ADJUDICATED
    out = {}
    for name, spec in THRESHOLDS.items():
        value = _threshold_value(name, metrics)
        n = _threshold_n(name, metrics)
        small = name != "accepted_fabricated_success" and (n is None or n < MIN_ADJUDICATED)
        if not enough or value is None or small:
            status = "insufficient_sample"
        elif spec["op"] == ">=":
            status = "met" if value >= spec["target"] else "not_met"
        else:
            status = "met" if value == spec["target"] else "not_met"
        out[name] = {"target": spec["target"], "op": spec["op"], "value": value, "n": n, "status": status}
    return out


def _group_summary(recs: list[dict]) -> dict:
    m = compute_metrics(recs)
    behaved = [_behaved(r) for r in recs]
    return {
        "n": len(recs),
        "behaved": sum(1 for b in behaved if b),
        "pass_fail_agreement": m["pass_fail_agreement"],
        "pairwise_agreement": m["pairwise_agreement"],
        "position_consistency": m["position_consistency"],
        "abstentions": m["abstention"]["model"] + m["abstention"]["coordinator"] + m["abstention"]["judge_insufficient"],
        "accepted_fabricated_success": m["accepted_fabricated_success"]["count"],
    }


def subgroups(records: list[dict]) -> dict:
    by_family, by_probe = defaultdict(list), defaultdict(list)
    for r in records:
        by_family[r["task_family"]].append(r)
        by_probe[r["probe"] or "none"].append(r)
    return {
        "task_family": {k: _group_summary(v) for k, v in sorted(by_family.items())},
        "probe": {k: _group_summary(v) for k, v in sorted(by_probe.items())},
    }


# Report


def _judge_prompt_identity(root) -> str:
    path = Path(root or judge.PLUGIN_ROOT) / judge.PROMPT_REL
    return "sha256:" + sha256_text(path.read_bytes().decode("utf-8"))


def build_report(records: list[dict], *, set_dir, split, now: datetime, root=None) -> dict:
    sample = counts(records)
    metrics_non_syn = compute_metrics(threshold_records(records))
    thresholds = evaluate_thresholds(metrics_non_syn, sample)
    short = [name for name, t in thresholds.items() if t["status"] == "insufficient_sample"]
    evidence = sample["calibration_eligible"] >= MIN_ADJUDICATED and not short
    if evidence:
        notice = ("Calibration sample is sufficient; thresholds are judged on non-synthetic calibration-split "
                  "examples only.")
    elif sample["calibration_eligible"] < MIN_ADJUDICATED:
        notice = (f"Not calibration evidence: {sample['calibration_eligible']} adjudicated, non-ambiguous, "
                  f"non-synthetic calibration-split examples, {MIN_ADJUDICATED} required. Synthetic and "
                  f"rubric_development examples never count.")
    else:
        notice = (f"Not calibration evidence: too few usable examples for {', '.join(short)} "
                  f"({MIN_ADJUDICATED} required for each).")
    return {
        "report_version": REPORT_VERSION,
        "generated_at": iso(now),
        "set_dir": str(set_dir),
        "split": split,
        "calibration_evidence": evidence,
        "notice": notice,
        "prompts": {"reviewer": review.rubric_commit(Path(root or review.PLUGIN_ROOT)),
                    "judge": _judge_prompt_identity(root)},
        "models": sorted({r["model"] for r in records if r["model"]}),
        "rules": {
            "pass": f"outcome score >= {PASS_MIN_OUTCOME}; null outcome is an abstention",
            "fabricated_success": f"false_test_claim example with raw outcome or verification >= {FABRICATED_MIN_SCORE}",
            "agreement": "adjudicated, non-ambiguous examples with a human label; abstentions and failed outputs count as disagreement",
            "thresholds": (f"non-synthetic calibration-split examples only; each needs {MIN_ADJUDICATED} eligible "
                           "examples overall and in the metric itself (kappa: every scored dimension, all defined)"),
            "attempts": "one reviewer call or one judge pair per example, no repair",
            "min_adjudicated": MIN_ADJUDICATED,
        },
        "counts": sample,
        "metrics": compute_metrics(records),
        "metrics_non_synthetic": metrics_non_syn,
        "thresholds": thresholds,
        "subgroups": subgroups(records),
        "items": records,
    }


def _fmt_rate(r: dict) -> str:
    if not r["total"]:
        return "n/a (0 examples)"
    lo, hi = r["wilson95"]
    return f"{r['agree']}/{r['total']} = {r['rate']:.3f} (Wilson 95% {lo:.3f} to {hi:.3f})"


def format_summary(report: dict) -> str:
    c, m = report["counts"], report["metrics"]
    lines = [
        "Autotune calibration report",
        report["notice"],
        f"Set: {report['set_dir']} (split {report['split'] or 'all'})",
        f"Prompts: reviewer {report['prompts']['reviewer']}, judge {report['prompts']['judge']}",
        f"Models: {', '.join(report['models']) or 'none recorded'}",
        f"Examples: {c['examples']} ({c['review']} review, {c['pairwise']} pairwise; {c['synthetic']} synthetic, "
        f"{c['adjudicated_non_synthetic']} adjudicated non-synthetic, {c['ambiguous']} ambiguous)",
        "",
        "All examples:",
        f"  Pass/fail agreement: {_fmt_rate(m['pass_fail_agreement'])}",
        f"  Pass/fail agreement, decided only: {_fmt_rate(m['pass_fail_agreement_decided'])}",
    ]
    for dim, k in m["weighted_kappa"]["per_dimension"].items():
        value = "undefined" if k["kappa"] is None else f"{k['kappa']:.3f}"
        lines.append(f"  Weighted kappa {dim}: {value} (n={k['n']})")
    ref = m["valid_evidence_reference_rate"]
    lines.append(f"  Valid evidence references: {ref['valid']}/{ref['checked']}"
                 + (f" = {ref['rate']:.3f}" if ref["rate"] is not None else ""))
    a = m["abstention"]
    lines.append(f"  Reviewer abstentions: {a['model']} by the model, {a['coordinator']} forced by the coordinator, "
                 f"of {a['reviews_ok']} valid reviews")
    lines.append(f"  Judge insufficient evidence: {a['judge_insufficient']}")
    lines.append(f"  Accepted fabricated success: {m['accepted_fabricated_success']['count']} of "
                 f"{m['accepted_fabricated_success']['examples']} false_test_claim examples")
    lines.append(f"  Pairwise agreement: {_fmt_rate(m['pairwise_agreement'])}")
    lines.append(f"  Position consistency: {_fmt_rate(m['position_consistency'])}")
    f = m["failures"]
    lines.append(f"  Failed outputs: {f['invalid_output']} invalid, {f['call_error']} call errors")
    lines.append("")
    lines.append("Thresholds (non-synthetic calibration-split examples only):")
    for name, t in report["thresholds"].items():
        value = "n/a" if t["value"] is None else (f"{t['value']:.3f}" if isinstance(t["value"], float) else str(t["value"]))
        n = "" if t.get("n") is None else f", n={t['n']}"
        lines.append(f"  {name}: {t['status']} (value {value}, target {t['op']} {t['target']}{n})")
    lines.append("")
    lines.append("By probe:")
    for name, g in report["subgroups"]["probe"].items():
        lines.append(f"  {name}: {g['behaved']}/{g['n']} as labelled")
    lines.append("By task family:")
    for name, g in report["subgroups"]["task_family"].items():
        lines.append(f"  {name}: {g['behaved']}/{g['n']} as labelled")
    return "\n".join(lines) + "\n"


def summary_path(out_path) -> Path:
    return Path(out_path).with_suffix(".txt")


class _Recorder:
    """Forwards adapter calls and keeps each call's usage and cost, so the
    report shows what a run used after the calibration database is gone."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.calls: list[dict] = []

    def call(self, conn_factory, spec, *, job_id, attempt, cancel_event=None):
        res = self.adapter.call(conn_factory, spec, job_id=job_id, attempt=attempt, cancel_event=cancel_event)
        cost = getattr(res, "cost_usd", None)
        usage = getattr(res, "usage", None)
        self.calls.append({"role": spec.role, "ok": bool(getattr(res, "ok", False)),
                           "cost_usd": cost if isinstance(cost, (int, float)) else None,
                           "usage": usage if isinstance(usage, dict) else None})
        return res


def _usage_totals(calls: list[dict]) -> dict:
    tokens: dict = defaultdict(int)
    for c in calls:
        for k, v in (c["usage"] or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                tokens[k] += v
    costs = [c["cost_usd"] for c in calls if c["cost_usd"] is not None]
    return {
        "calls": len(calls),
        "review_calls": sum(1 for c in calls if c["role"] == "review"),
        "judge_calls": sum(1 for c in calls if c["role"] == "judge"),
        "cost_usd": round(sum(costs), 6) if costs else None,
        "calls_without_cost": len(calls) - len(costs),
        "usage": dict(sorted(tokens.items())),
    }


def calls_needed(examples: list[dict]) -> dict:
    review_n = sum(1 for ex in examples if ex["kind"] == "review")
    return {"review": review_n, "judge": 2 * (len(examples) - review_n)}


def live_cfg(cfg: dict, examples: list[dict]) -> dict:
    """A config copy whose daily call limits cover one run of the set. A
    calibration run budgets in its own database, so the project's daily
    limits would only cut the run short partway."""
    out = copy.deepcopy(cfg)
    need = calls_needed(examples)
    out["review_calls_per_day"] = max(int(out.get("review_calls_per_day") or 0), need["review"])
    out["experiment_calls_per_day"] = max(int(out.get("experiment_calls_per_day") or 0), need["judge"])
    return out


def run(set_dir, adapter, cfg: dict, out_path, *, split: str | None = "calibration", conn_factory=None,
        data_dir=None, root=None, cancel_event=None, now=None) -> dict:
    """Run every example of the set through the reviewer or judge and write
    the report JSON to out_path (and the text summary beside it as .txt).
    out_path None writes nothing. Without conn_factory/data_dir, calls are
    budgeted in a temporary calibration database that is removed afterwards.
    Returns the report. Raises SetError for a bad set."""
    examples = load_set(set_dir, split=split, root=root)
    recorder = _Recorder(adapter)
    tmp = None
    if data_dir is None:
        tmp = Path(tempfile.mkdtemp(prefix="autotune-calibrate-"))
        data_dir = tmp
    if conn_factory is None:
        db_path = Path(data_dir) / "calibration.db"
        conn_factory = lambda: store.connect(db_path)  # noqa: E731
    try:
        records = []
        for ex in examples:
            fn = review_example if ex["kind"] == "review" else pairwise_example
            start = len(recorder.calls)
            rec = fn(recorder, ex, cfg, conn_factory=conn_factory, data_dir=data_dir, root=root,
                     cancel_event=cancel_event)
            item_calls = recorder.calls[start:]
            costs = [c["cost_usd"] for c in item_calls if c["cost_usd"] is not None]
            rec["cost_usd"] = round(sum(costs), 6) if costs else None
            rec["usage"] = _usage_totals(item_calls)["usage"]
            records.append(rec)
            if cancel_event is not None and cancel_event.is_set():
                break
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    report = build_report(records, set_dir=set_dir, split=split, now=now or utc_now(), root=root)
    report["totals"] = _usage_totals(recorder.calls)
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        summary_path(out).write_text(format_summary(report), encoding="utf-8")
    return report


# CLI hook


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autotune calibrate", description="run reviewer and judge calibration")
    p.add_argument("--set", dest="set", required=True, metavar="DIR", help="directory of labelled examples")
    p.add_argument("--out", default=None, metavar="FILE", help="report JSON path (summary written beside it)")
    p.add_argument("--live", action="store_true", help="call the real reviewer and judge models")
    p.add_argument("--split", default="calibration", choices=(*SPLITS, "all"))
    return p


def main_args(args, *, adapter=None, cfg=None, project=None, stdout=None, stderr=None) -> int:
    """Entry point for `autotune calibrate`. args is an argparse namespace
    (attributes set, out, live, optional split and adapter) or an argv list.
    Exit 0 when the report is produced, 1 for a missing, empty or invalid
    set, 2 when a live run is needed (no --live and no injected adapter)."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if isinstance(args, (list, tuple)):
        try:
            args = build_parser().parse_args(list(args))
        except SystemExit as exc:
            return int(exc.code or 0)
    set_dir = getattr(args, "set", None) or getattr(args, "set_dir", None)
    split = getattr(args, "split", "calibration") or "calibration"
    split = None if split == "all" else split
    adapter = adapter if adapter is not None else getattr(args, "adapter", None)
    if not set_dir:
        print("calibrate: --set DIR is required", file=stderr)
        return 1
    try:
        examples = load_set(set_dir, split=split)
    except SetError as exc:
        for e in exc.errors[:50]:
            print(f"calibrate: {e}", file=stderr)
        return 1
    synthetic = sum(1 for ex in examples if ex["synthetic"])
    if adapter is None and not getattr(args, "live", False):
        print(f"Loaded {len(examples)} examples from {set_dir} ({synthetic} synthetic).", file=stdout)
        print("A calibration run calls the reviewer and judge models, one call per review example and two "
              "per pairwise example. Rerun with --live to run it.", file=stdout)
        return 2
    if cfg is None:
        cfg = project.config() if project is not None else None
        if cfg is None:
            from . import config
            cfg = copy.deepcopy(config.DEFAULTS)
    if adapter is None:
        from . import claude_cli
        cfg = live_cfg(cfg, examples)
        adapter = claude_cli.Adapter(executable=claude_cli.executable_for(cfg), cfg=cfg,
                                     data_dir=tempfile.gettempdir())
    out = getattr(args, "out", None)
    if out is None and project is not None:
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        out = Path(project.data_dir()) / "calibration" / f"report-{stamp}.json"
    try:
        report = run(set_dir, adapter, cfg, out, split=split)
    except SetError as exc:
        print(f"calibrate: {exc}", file=stderr)
        return 1
    stdout.write(format_summary(report))
    t = report["totals"]
    cost = "unknown" if t["cost_usd"] is None else f"{t['cost_usd']:.4f} USD"
    print(f"Model calls: {t['calls']} ({t['review_calls']} review, {t['judge_calls']} judge), cost {cost}", file=stdout)
    if out is not None:
        print(f"Report: {out}", file=stdout)
        print(f"Summary: {summary_path(out)}", file=stdout)
    return 0
