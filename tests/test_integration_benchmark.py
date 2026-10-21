"""Foreground capture latency. Results go to stderr and benchmarks/results/.

The first benchmark times 200 sequential turn_complete ingress runs in a tiny
repository with no worker. The second times what a real turn costs: a
turn_start ingress plus a near-cap turn_complete, against a repository with
many tracked files, while a spawned worker polls the same database.

Targets (docs/IMPLEMENTATION_PLAN.md, M1): p95 150 ms, p99 500 ms. Only the
1000 ms fail-open timeout is asserted; target misses are reported, not hidden.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
from datetime import datetime, timezone

from tests._integration_support import PLUGIN_ROOT, IntegrationCase
from tests._runtime_support import git

RUNS = 200
RESULTS = PLUGIN_ROOT / "benchmarks" / "results" / "capture-latency.json"
TURN_RESULTS = PLUGIN_ROOT / "benchmarks" / "results" / "capture-latency-turn.json"
TURN_RUNS = 60
TRACKED_FILES = 3000
# The mod keeps envelopes under 500 KiB; aim just below that.
NEAR_CAP_BYTES = 480 * 1024
P95_TARGET_MS = 150
P99_TARGET_MS = 500
FAIL_OPEN_MS = 1000


def percentile(sorted_ms: list[float], p: float) -> float:
    k = (len(sorted_ms) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_ms) - 1)
    return sorted_ms[lo] + (sorted_ms[hi] - sorted_ms[lo]) * (k - lo)


class CaptureLatencyBenchmark(IntegrationCase):
    def test_sequential_ingress_latency(self):
        samples = []
        for i in range(RUNS):
            env = self.envelope(session_id="bench-s", turn_id=f"bench-t{i:04d}")
            code, ack, out, elapsed = self.run_ingress(env)
            self.assertEqual(code, 0, out)
            self.assertFalse(ack["duplicate"])
            samples.append(elapsed * 1000)
        ms = sorted(samples)
        result = {
            "runs": RUNS,
            "p50_ms": round(percentile(ms, 50), 1),
            "p95_ms": round(percentile(ms, 95), 1),
            "p99_ms": round(percentile(ms, 99), 1),
            "max_ms": round(ms[-1], 1),
            "mean_ms": round(statistics.fmean(ms), 1),
            "p95_target_ms": P95_TARGET_MS,
            "p99_target_ms": P99_TARGET_MS,
            "p95_meets_target": percentile(ms, 95) < P95_TARGET_MS,
            "p99_meets_target": percentile(ms, 99) < P99_TARGET_MS,
            "measured": "wall time of subprocess.run, including interpreter start, AUTOTUNE_NO_WORKER=1",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        os.makedirs(RESULTS.parent, exist_ok=True)
        RESULTS.write_text(json.dumps(result, indent=2) + "\n")
        print(
            f"\ncapture latency over {RUNS} runs: p50 {result['p50_ms']} ms, p95 {result['p95_ms']} ms "
            f"(target {P95_TARGET_MS}: {'met' if result['p95_meets_target'] else 'MISSED'}), "
            f"p99 {result['p99_ms']} ms (target {P99_TARGET_MS}: "
            f"{'met' if result['p99_meets_target'] else 'MISSED'}), max {result['max_ms']} ms; written to {RESULTS}",
            file=sys.stderr,
        )
        self.assertLess(result["p99_ms"], FAIL_OPEN_MS)


def _summary(samples_ms: list[float]) -> dict:
    ms = sorted(samples_ms)
    return {
        "p50_ms": round(percentile(ms, 50), 1),
        "p95_ms": round(percentile(ms, 95), 1),
        "p99_ms": round(percentile(ms, 99), 1),
        "max_ms": round(ms[-1], 1),
    }


class TurnLatencyBenchmark(IntegrationCase):
    def _near_cap_envelope(self, turn_id: str) -> dict:
        env = self.envelope(session_id="bench-turn", turn_id=turn_id)
        call = {"tool": "Bash", "tool_use_id": "", "input_summary": "i" * 2000, "is_error": False,
                "result_excerpt": "r" * 2000, "denied": False}
        calls = []
        while len(json.dumps({**env, "tool_calls": calls})) < NEAR_CAP_BYTES - 4200 and len(calls) < 200:
            calls.append({**call, "tool_use_id": f"tu{len(calls)}"})
        env["tool_calls"] = calls
        env["answer_text"] = "a" * max(0, NEAR_CAP_BYTES - len(json.dumps(env)))
        return env

    def test_turn_start_plus_large_turn_complete_with_worker(self):
        files = self.repo / "files"
        files.mkdir()
        for i in range(TRACKED_FILES):
            (files / f"f{i:05d}.txt").write_text(f"{i}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "many files")
        env = self.sub_env(worker=True)

        starts, completes, turns = [], [], []
        size = 0
        for i in range(TURN_RUNS):
            turn_id = f"turn-{i:04d}"
            code, ack, out, t_start = self.run_ingress(self.envelope("turn_start", session_id="bench-turn",
                                                                     turn_id=turn_id), env=env)
            self.assertEqual(code, 0, out)
            big = self._near_cap_envelope(turn_id)
            size = len(json.dumps(big).encode("utf-8"))
            code, ack, out, t_complete = self.run_ingress(big, env=env)
            self.assertEqual(code, 0, out)
            self.assertFalse(ack["duplicate"])
            starts.append(t_start * 1000)
            completes.append(t_complete * 1000)
            turns.append((t_start + t_complete) * 1000)

        per_turn = _summary(turns)
        result = {
            "runs": TURN_RUNS,
            "turn_start": _summary(starts),
            "turn_complete": _summary(completes),
            "per_turn": per_turn,
            "per_turn_p95_meets_target": per_turn["p95_ms"] < P95_TARGET_MS,
            "per_turn_p99_meets_target": per_turn["p99_ms"] < P99_TARGET_MS,
            "p95_target_ms": P95_TARGET_MS,
            "p99_target_ms": P99_TARGET_MS,
            "envelope_bytes": size,
            "tracked_files": TRACKED_FILES + 1,
            "measured": "wall time of turn_start plus turn_complete ingress, worker spawning enabled",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        os.makedirs(TURN_RESULTS.parent, exist_ok=True)
        TURN_RESULTS.write_text(json.dumps(result, indent=2) + "\n")
        print(
            f"\nper-turn capture latency over {TURN_RUNS} turns ({size} byte envelope, worker running): "
            f"turn_start p95 {result['turn_start']['p95_ms']} ms, turn_complete p95 "
            f"{result['turn_complete']['p95_ms']} ms, per turn p95 {per_turn['p95_ms']} ms "
            f"(target {P95_TARGET_MS}: {'met' if result['per_turn_p95_meets_target'] else 'MISSED'}), "
            f"p99 {per_turn['p99_ms']} ms (target {P99_TARGET_MS}: "
            f"{'met' if result['per_turn_p99_meets_target'] else 'MISSED'}); written to {TURN_RESULTS}",
            file=sys.stderr,
        )
        self.assertLess(result["turn_start"]["p99_ms"], FAIL_OPEN_MS)
        self.assertLess(result["turn_complete"]["p99_ms"], FAIL_OPEN_MS)
