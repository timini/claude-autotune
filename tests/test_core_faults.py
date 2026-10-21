"""Test-only crash points: honoured only under a test data dir with an explicit opt-in."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from autotune import faults, worker

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = (
    "import sys\n"
    "from autotune import worker\n"
    "worker.fault_point(sys.argv[1])\n"
    "print('survived')\n"
)


def run_point(name: str, env: dict) -> subprocess.CompletedProcess:
    base = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-c", _SCRIPT, name], cwd=ROOT, env={**base, **env},
                          capture_output=True, text=True, timeout=30)


class FaultPointTest(unittest.TestCase):
    ENABLED = {"AUTOTUNE_HOME": "/nonexistent/autotune-home", "AUTOTUNE_TEST_FAULTS_ENABLED": "1"}

    def test_armed_point_ends_the_process_with_code_70(self):
        out = run_point("apply_after_merge", {**self.ENABLED, "AUTOTUNE_TEST_FAULT": "apply_after_merge"})
        self.assertEqual(out.returncode, faults.FAULT_EXIT_CODE)
        self.assertEqual(out.stdout, "")

    def test_other_points_pass(self):
        out = run_point("apply_before_merge", {**self.ENABLED, "AUTOTUNE_TEST_FAULT": "apply_after_merge"})
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "survived"))

    def test_ignored_without_test_home_or_opt_in(self):
        for env in (
            {"AUTOTUNE_TEST_FAULT": "apply_after_merge", "AUTOTUNE_TEST_FAULTS_ENABLED": "1"},
            {"AUTOTUNE_TEST_FAULT": "apply_after_merge", "AUTOTUNE_HOME": "/x"},
            {"AUTOTUNE_TEST_FAULT": "apply_after_merge", "AUTOTUNE_HOME": "/x", "AUTOTUNE_TEST_FAULTS_ENABLED": "true"},
        ):
            out = run_point("apply_after_merge", env)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, "survived"), env)

    def test_every_contract_point_is_known_and_unknown_names_raise(self):
        self.assertEqual(faults.FAULT_POINTS, {"apply_before_merge", "apply_after_merge", "apply_after_record",
                                               "revert_after_commit", "revert_after_merge"})
        with self.assertRaises(ValueError):
            faults.fault_point("apply_after_mrege", {})

    def test_several_points_and_injected_exit(self):
        calls = []
        env = {**self.ENABLED, "AUTOTUNE_TEST_FAULT": "revert_after_commit, revert_after_merge"}
        for name in sorted(faults.FAULT_POINTS):
            faults.fault_point(name, env, _exit=calls.append)
        self.assertEqual(calls, [70, 70])
        self.assertIs(worker.fault_point, faults.fault_point)


if __name__ == "__main__":
    unittest.main()
