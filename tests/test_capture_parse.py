import json
import time
import unittest

from autotune.capture import MAX_ENVELOPE_BYTES, EnvelopeError, parse_envelope


def turn_complete(**overrides):
    env = {
        "schema": "autotune.envelope/1",
        "kind": "turn_complete",
        "origin": "foreground",
        "session_id": "sess-1",
        "cwd": "/home/user/project",
        "sent_at": "2026-10-21T10:00:05.000Z",
        "turn_id": "turn-1",
        "lifecycle_id": "life-1",
        "lifecycle_source": "startup",
        "transcript_path": "/home/user/.claude/t.jsonl",
        "started_at": "2026-10-21T10:00:00.000Z",
        "completed_at": "2026-10-21T10:00:04.000Z",
        "end_reason": "answer",
        "refusal": None,
        "duration_ms": 4000,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "model": "claude-test",
        "request_text": "Fix the failing test",
        "answer_text": "Done, the test passes now.",
        "tool_calls": [
            {"tool": "Bash", "tool_use_id": "tu1", "input_summary": "pytest -q",
             "is_error": False, "result_excerpt": "1 passed", "denied": False},
        ],
        "skills": [{"skill": "tdd", "source": "project", "path": "/p/SKILL.md", "plugin_name": None}],
        "instructions": [{"file_path": "/p/CLAUDE.md", "memory_type": "Project",
                          "load_reason": "session_start"}],
        "coverage": {"truncated_fields": [], "dropped_tool_calls": 0,
                     "missing_turn_start": False, "capture_errors": []},
    }
    env.update(overrides)
    return env


def encode(obj):
    return json.dumps(obj).encode("utf-8")


class ParseValidTests(unittest.TestCase):
    def test_valid_turn_complete_round_trips_fields(self):
        env = parse_envelope(encode(turn_complete()))
        self.assertEqual(env["kind"], "turn_complete")
        self.assertEqual(env["session_id"], "sess-1")
        self.assertEqual(env["tool_calls"][0]["result_excerpt"], "1 passed")
        self.assertEqual(env["_unknown_fields"], [])

    def test_optional_fields_default(self):
        raw = turn_complete()
        for key in ("transcript_path", "started_at", "refusal", "duration_ms", "usage", "model",
                    "request_text"):
            del raw[key]
        raw["tool_calls"] = [{"tool": "Read", "tool_use_id": "tu9", "input_summary": "a.py"}]
        raw["coverage"] = {}
        env = parse_envelope(encode(raw))
        self.assertIsNone(env["request_text"])
        self.assertIsNone(env["started_at"])
        call = env["tool_calls"][0]
        self.assertIsNone(call["result_excerpt"])
        self.assertIsNone(call["is_error"])
        self.assertFalse(call["denied"])
        self.assertEqual(env["coverage"]["dropped_tool_calls"], 0)
        self.assertFalse(env["coverage"]["missing_turn_start"])

    def test_unknown_fields_are_listed_not_rejected(self):
        raw = turn_complete(future_field=1, reason="belongs to session_end")
        raw["tool_calls"][0]["extra"] = True
        env = parse_envelope(encode(raw))
        self.assertEqual(env["_unknown_fields"], ["future_field", "reason", "tool_calls[].extra"])
        self.assertNotIn("future_field", env)

    def test_non_foreground_origin_parses(self):
        env = parse_envelope(encode(turn_complete(origin="worker")))
        self.assertEqual(env["origin"], "worker")

    def test_turn_start_and_session_end(self):
        start = parse_envelope(encode({
            "schema": "autotune.envelope/1", "kind": "turn_start", "origin": "foreground",
            "session_id": "s", "cwd": "/x", "sent_at": "2026-10-21T10:00:00Z",
            "turn_id": "t", "request_text": "", "lifecycle_id": "l", "lifecycle_source": "clear",
        }))
        self.assertEqual(start["request_text"], "")
        end = parse_envelope(encode({
            "schema": "autotune.envelope/1", "kind": "session_end", "origin": "foreground",
            "session_id": "s", "cwd": "/x", "sent_at": "2026-10-21T10:00:00Z", "reason": "logout",
        }))
        self.assertEqual(end["reason"], "logout")


class ParseRejectTests(unittest.TestCase):
    def assertCode(self, raw, code):
        with self.assertRaises(EnvelopeError) as ctx:
            parse_envelope(raw)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_too_large_rejected_before_decode_or_parse(self):
        # Not UTF-8 and not JSON: only a size check that runs first can give this code.
        self.assertCode(b"\xff" * (600 * 1024), "envelope_too_large")

    def test_too_large_still_recovers_identity(self):
        raw = turn_complete(answer_text="x" * (MAX_ENVELOPE_BYTES + 10))
        err = self.assertCode(encode(raw), "envelope_too_large")
        self.assertEqual(err.identity, {"session_id": "sess-1", "turn_id": "turn-1"})

    def test_exactly_max_size_is_not_too_large(self):
        raw = encode(turn_complete())
        padded = raw + b" " * (MAX_ENVELOPE_BYTES - len(raw))
        self.assertEqual(len(padded), MAX_ENVELOPE_BYTES)
        self.assertEqual(parse_envelope(padded)["kind"], "turn_complete")

    def test_invalid_utf8(self):
        raw = encode(turn_complete()).replace(b"Fix the", b"Fix \xc3\x28he")
        self.assertCode(raw, "invalid_encoding")

    def test_lone_surrogate_escape_rejected(self):
        raw = encode(turn_complete()).replace(b"Fix the", b"Fix \\udc80the")
        self.assertCode(raw, "invalid_encoding")

    def test_deep_nesting_rejected_quickly(self):
        raw = b"[" * 10000 + b"]" * 10000
        start = time.perf_counter()
        self.assertCode(raw, "nesting_too_deep")
        self.assertLess(time.perf_counter() - start, 0.5)

    def test_deep_nesting_inside_usage_rejected(self):
        env = encode(turn_complete(usage=None))
        nested = b'{"a":' * 5000 + b"1" + b"}" * 5000
        raw = env.replace(b'"usage": null', b'"usage": ' + nested)
        self.assertCode(raw, "nesting_too_deep")

    def test_brackets_inside_strings_do_not_count_as_depth(self):
        env = parse_envelope(encode(turn_complete(answer_text="[" * 5000 + '\\"{' * 100)))
        self.assertTrue(env["answer_text"].startswith("[[["))

    def test_many_quotes_and_escapes_stay_fast(self):
        raw = b'{"a":"' + b'\\"' * 200000 + b'"}'
        start = time.perf_counter()
        self.assertCode(raw, "missing_field")
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_invalid_json(self):
        self.assertCode(b'{"schema": ', "invalid_json")
        self.assertCode(b"[1, 2]", "invalid_json")
        self.assertCode(b'{"x": NaN}', "invalid_json")

    def test_wrong_schema(self):
        self.assertCode(encode(turn_complete(schema="autotune.envelope/2")), "unsupported_schema")

    def test_missing_required_field_names_it(self):
        raw = turn_complete()
        del raw["answer_text"]
        err = self.assertCode(encode(raw), "missing_field")
        self.assertEqual(err.field, "answer_text")
        self.assertEqual(err.identity, {"session_id": "sess-1", "turn_id": "turn-1"})

    def test_field_violations(self):
        cases = [
            {"kind": "turn_done"},
            {"origin": "elsewhere"},
            {"end_reason": "success"},
            {"lifecycle_source": "boot"},
            {"cwd": "relative/path"},
            {"sent_at": "yesterday"},
            {"duration_ms": -1},
            {"duration_ms": True},
            {"request_text": "x" * 32001},
            {"answer_text": "x" * 64001},
            {"tool_calls": [{"tool": "Bash", "tool_use_id": "t"}] * 201},
            {"session_id": ""},
            {"usage": [1, 2]},
        ]
        for override in cases:
            with self.subTest(override=list(override)[0]):
                with self.assertRaises(EnvelopeError) as ctx:
                    parse_envelope(encode(turn_complete(**override)))
                self.assertIn(ctx.exception.code, {"invalid_field", "missing_field"})

    def test_tool_call_item_violations(self):
        raw = turn_complete()
        raw["tool_calls"][0]["input_summary"] = "x" * 2001
        err = self.assertCode(encode(raw), "invalid_field")
        self.assertEqual(err.field, "tool_calls[0].input_summary")


if __name__ == "__main__":
    unittest.main()


class TimestampOverflowTest(unittest.TestCase):
    def test_overflowing_offset_is_an_envelope_error(self):
        raw = json.dumps(turn_complete(sent_at="9999-12-31T23:59:59-01:00")).encode("utf-8")
        with self.assertRaises(EnvelopeError) as ctx:
            parse_envelope(raw)
        self.assertEqual(ctx.exception.code, "invalid_field")
