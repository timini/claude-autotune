"""Malformed and oversized input through a real ingress process stays within
time limits, gets the right exit code, and leaves a capture error row when
the identity can be recovered."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from autotune import capture
from tests._integration_support import PLUGIN_ROOT, PYTHON, IntegrationCase

LIMIT_SECONDS = 2.0


class BoundedInputTest(IntegrationCase):
    def _head(self, sid: str) -> bytes:
        """Identity fields first, as the mod writes them, so the bounded raw
        scan can find them."""
        return json.dumps({"schema": "autotune.envelope/1", "kind": "turn_complete", "origin": "foreground",
                           "session_id": sid, "turn_id": "t-1", "cwd": str(self.repo)})[:-1].encode()

    def _check(self, raw: bytes, exit_code: int, error: str | None, sid: str):
        code, ack, out, elapsed = self.run_ingress(raw, timeout=10)
        self.assertLess(elapsed, LIMIT_SECONDS, f"{sid} took {elapsed:.2f} s")
        self.assertEqual(code, exit_code, out)
        if error is None:
            self.assertTrue(ack["ok"], ack)
        else:
            self.assertEqual(ack["ok"], False)
            self.assertEqual(ack["error"], error)
            rows = self.query("SELECT code, turn_id FROM capture_errors WHERE session_id = ?", (sid,))
            self.assertEqual([(r["code"], r["turn_id"]) for r in rows], [(error, "t-1")])
        return ack

    def test_ten_megabytes_of_stdin(self):
        raw = self._head("s-big") + b', "answer_text": "' + b"x" * (10 * 1024 * 1024) + b'"}'
        self._check(raw, 2, "envelope_too_large", "s-big")

    def test_ten_megabytes_peak_memory(self):
        raw = self._head("s-mem") + b', "answer_text": "' + b"x" * (10 * 1024 * 1024) + b'"}'
        proc = subprocess.Popen([PYTHON, "-m", "autotune", "ingress"], cwd=PLUGIN_ROOT, env=self.sub_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.stdin.write(raw)
            proc.stdin.close()
        except BrokenPipeError:
            pass  # ingress stops reading at its bound and may exit first
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        _, status, usage = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)
        self.assertEqual(proc.returncode, 2)
        peak = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
        print(f"\ningress peak RSS on 10 MB stdin: {peak / 1048576:.1f} MiB", file=sys.stderr)
        self.assertLess(peak, 128 * 1024 * 1024)

    def test_invalid_utf8(self):
        raw = self._head("s-utf8") + b', "answer_text": "bad \xff\xfe bytes"}'
        self._check(raw, 2, "invalid_encoding", "s-utf8")

    def test_deep_nesting(self):
        raw = self._head("s-deep") + b', "usage": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
        self._check(raw, 2, "nesting_too_deep", "s-deep")

    def test_unterminated_json(self):
        raw = self._head("s-trunc") + b', "answer_text": "never closed'
        self._check(raw, 2, "invalid_json", "s-trunc")

    def _tool_calls(self, chars: int) -> list[dict]:
        return [
            {"tool": "Bash", "tool_use_id": f"toolu_{i:04d}", "input_summary": "i" * chars,
             "is_error": False, "result_excerpt": "r" * chars, "denied": False}
            for i in range(200)
        ]

    def test_200_max_size_tool_calls_exceed_envelope_cap(self):
        env = self.envelope(session_id="s-tools-max", turn_id="t-1", tool_calls=self._tool_calls(2000))
        raw = json.dumps(env).encode()
        # 200 calls of 2000 + 2000 chars cannot fit in 512 KiB. The mod drops
        # the oldest calls first; ingress must reject, not truncate silently.
        self.assertGreater(len(raw), capture.MAX_ENVELOPE_BYTES)
        self._check(raw, 2, "envelope_too_large", "s-tools-max")

    def test_200_tool_calls_that_fit_are_accepted(self):
        env = self.envelope(session_id="s-tools", turn_id="t-1", tool_calls=self._tool_calls(1200))
        raw = json.dumps(env).encode()
        self.assertLess(len(raw), capture.MAX_ENVELOPE_BYTES)
        ack = self._check(raw, 0, None, "s-tools")
        kinds = self.query("SELECT kind, COUNT(*) AS n FROM evidence WHERE episode_id = ? GROUP BY kind",
                           (ack["episode_id"],))
        counts = {r["kind"]: r["n"] for r in kinds}
        self.assertEqual(counts["tool_call"], 200)
        self.assertEqual(counts["tool_result"], 200)
