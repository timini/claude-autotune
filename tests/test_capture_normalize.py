import copy
import json
import re
import unittest
from datetime import datetime, timezone

from autotune.capture import (MAX_EVIDENCE_CHARS, MEDIA_PLACEHOLDER, EnvelopeError,
                              normalize_episode, parse_envelope)
from autotune.util import sha256_text
from tests.test_capture_parse import encode, turn_complete

NOW = datetime(2026, 10, 21, 10, 0, 6, tzinfo=timezone.utc)
REPO = {"toplevel": "/home/user/project", "branch": "main", "head": "abc123", "tracked_dirty": False}


def normalize(**overrides):
    env = parse_envelope(encode(turn_complete(**overrides)))
    return normalize_episode(env, REPO, NOW)


def two_tool_env(**overrides):
    calls = [
        {"tool": "Read", "tool_use_id": "tuA", "input_summary": "a.py",
         "is_error": False, "result_excerpt": "contents", "denied": False},
        {"tool": "Bash", "tool_use_id": "tuB", "input_summary": "rm -rf build",
         "is_error": None, "result_excerpt": None, "denied": True},
        {"tool": "Bash", "tool_use_id": "tuC", "input_summary": "make",
         "is_error": True, "result_excerpt": "error 2", "denied": False},
    ]
    return normalize(tool_calls=calls, **overrides)


class EvidenceOrderTests(unittest.TestCase):
    def test_documented_order_and_ids(self):
        _, evidence = two_tool_env()
        kinds = [e["kind"] for e in evidence]
        self.assertEqual(kinds, [
            "user_request",
            "tool_call", "tool_call", "tool_call",
            "tool_result", "tool_result",
            "skill_load", "instruction_load",
            "assistant_answer", "end_reason",
        ])
        self.assertEqual([e["evidence_id"] for e in evidence],
                         [f"e{i:04d}" for i in range(1, len(evidence) + 1)])
        positions = [e["position"] for e in evidence]
        self.assertEqual(positions, sorted(set(positions)))

    def test_tool_results_reference_their_calls(self):
        _, evidence = two_tool_env()
        results = [e for e in evidence if e["kind"] == "tool_result"]
        refs = [json.loads(e["provenance_json"])["tool_use_id"] for e in results]
        self.assertEqual(refs, ["tuA", "tuC"])
        self.assertEqual([e["content"] for e in results], ["contents", "error 2"])
        self.assertTrue(all(e["role"] == "tool" for e in results))
        calls = [json.loads(e["provenance_json"]) for e in evidence if e["kind"] == "tool_call"]
        self.assertEqual([c["denied"] for c in calls], [False, True, False])

    def test_every_row_hash_matches_stored_content(self):
        _, evidence = two_tool_env()
        for row in evidence:
            self.assertEqual(row["content_sha256"], sha256_text(row["content"]))
            json.loads(row["provenance_json"])
            self.assertIn(row["role"], {"user", "assistant", "tool", "system"})

    def test_answer_and_end_reason_always_present(self):
        _, evidence = normalize(answer_text="", tool_calls=[], skills=[], instructions=[])
        self.assertEqual([e["kind"] for e in evidence],
                         ["user_request", "assistant_answer", "end_reason"])
        self.assertEqual(evidence[1]["content"], "")
        self.assertEqual(evidence[2]["content"], "answer")


class EpisodeRowTests(unittest.TestCase):
    def test_episode_row_fields(self):
        episode, _ = normalize()
        self.assertTrue(episode["episode_id"].startswith("ep_"))
        self.assertEqual(episode["session_id"], "sess-1")
        self.assertEqual(episode["turn_id"], "turn-1")
        self.assertEqual(episode["completed_at"], "2026-10-21T10:00:04.000Z")
        self.assertEqual(episode["captured_at"], "2026-10-21T10:00:06.000Z")
        self.assertIsNone(episode["task_id"])
        self.assertEqual(json.loads(episode["repository_json"]), REPO)
        self.assertEqual(json.loads(episode["usage_json"]), {"input_tokens": 10, "output_tokens": 5})
        harness = json.loads(episode["loaded_harness_json"])
        self.assertEqual(harness["exposure"], "observed")
        self.assertEqual(harness["skills"][0]["skill"], "tdd")

    def test_exposure_unknown_without_loads(self):
        episode, _ = normalize(skills=[], instructions=[])
        self.assertEqual(json.loads(episode["loaded_harness_json"])["exposure"], "unknown")

    def test_episode_ids_are_unique(self):
        env = parse_envelope(encode(turn_complete()))
        a, _ = normalize_episode(env, REPO, NOW)
        b, _ = normalize_episode(env, REPO, NOW)
        self.assertNotEqual(a["episode_id"], b["episode_id"])

    def test_input_envelope_not_mutated(self):
        env = parse_envelope(encode(turn_complete()))
        before = copy.deepcopy(env)
        normalize_episode(env, REPO, NOW)
        self.assertEqual(env, before)

    def test_rejects_other_kinds(self):
        env = parse_envelope(encode({
            "schema": "autotune.envelope/1", "kind": "session_end", "origin": "foreground",
            "session_id": "s", "cwd": "/x", "sent_at": "2026-10-21T10:00:00Z", "reason": "exit",
        }))
        with self.assertRaises(EnvelopeError):
            normalize_episode(env, REPO, NOW)

    def test_success_is_never_inferred(self):
        episode, evidence = normalize(end_reason="unknown")
        self.assertEqual(episode["end_reason"], "unknown")
        self.assertEqual(evidence[-1]["content"], "unknown")
        self.assertNotIn("review_status", episode)


class CoverageTests(unittest.TestCase):
    def test_missing_request_text(self):
        episode, evidence = normalize(request_text=None)
        self.assertNotIn("user_request", [e["kind"] for e in evidence])
        self.assertEqual(evidence[0]["evidence_id"], "e0001")
        self.assertTrue(json.loads(episode["coverage_json"])["missing_turn_start"])

    def test_mod_flag_missing_turn_start_preserved(self):
        coverage = {"missing_turn_start": True, "truncated_fields": ["answer_text"],
                    "dropped_tool_calls": 3, "capture_errors": ["late start"]}
        episode, _ = normalize(coverage=coverage)
        cov = json.loads(episode["coverage_json"])
        self.assertTrue(cov["missing_turn_start"])
        self.assertEqual(cov["truncated_fields"], ["answer_text"])
        self.assertEqual(cov["dropped_tool_calls"], 3)
        self.assertEqual(cov["capture_errors"], ["late start"])

    def test_refusal_recorded(self):
        episode, evidence = normalize(
            end_reason="refusal",
            refusal={"category": "cyber", "explanation": "declined to write malware"})
        self.assertEqual(episode["end_reason"], "refusal")
        end = evidence[-1]
        self.assertEqual(end["kind"], "end_reason")
        self.assertTrue(end["content"].startswith("refusal: cyber"))
        self.assertIn("declined to write malware", end["content"])

    def test_refusal_without_category(self):
        _, evidence = normalize(end_reason="refusal", refusal=None)
        self.assertEqual(evidence[-1]["content"], "refusal: unknown")

    def test_long_content_truncated_and_flagged(self):
        answer = "start " + "x" * 20000 + " the conclusion"
        episode, evidence = normalize(answer_text=answer)
        row = next(e for e in evidence if e["kind"] == "assistant_answer")
        self.assertLessEqual(len(row["content"]), MAX_EVIDENCE_CHARS)
        self.assertEqual(row["truncated"], 1)
        self.assertTrue(row["content"].startswith("start "))
        self.assertTrue(row["content"].endswith("the conclusion"))
        self.assertIn(row["evidence_id"], json.loads(episode["coverage_json"])["evidence_truncated"])
        others = [e for e in evidence if e is not row]
        self.assertTrue(all(e["truncated"] == 0 for e in others))

    def test_unknown_fields_carried_into_coverage(self):
        episode, _ = normalize(brand_new=1)
        self.assertEqual(json.loads(episode["coverage_json"])["unknown_fields"], ["brand_new"])


class RedactionAndMediaTests(unittest.TestCase):
    AWS = "AKIAIOSFODNN7EXAMPLE"
    GH = "ghp_" + "a1B2" * 9

    def test_secrets_redacted_and_counted(self):
        calls = [{"tool": "Bash", "tool_use_id": "tu1", "input_summary": "env",
                  "is_error": False, "result_excerpt": f"AWS_KEY={self.AWS}\nok",
                  "denied": False}]
        episode, evidence = normalize(
            tool_calls=calls, answer_text=f"Use token {self.GH} to push.")
        blob = "\n".join(e["content"] for e in evidence)
        self.assertNotIn(self.AWS, blob)
        self.assertNotIn(self.GH, blob)
        self.assertIn("[REDACTED:aws_access_key]", blob)
        self.assertIn("[REDACTED:github_token]", blob)
        self.assertEqual(json.loads(episode["coverage_json"])["redactions"], 2)
        for row in evidence:
            self.assertEqual(row["content_sha256"], sha256_text(row["content"]))

    def test_no_secrets_means_zero_redactions(self):
        episode, _ = normalize()
        cov = json.loads(episode["coverage_json"])
        self.assertEqual(cov["redactions"], 0)
        self.assertFalse(cov["unsupported_media"])

    def test_data_uri_flagged_unsupported_media(self):
        uri = "data:image/png;base64," + "iVBORw0KGgo" * 50
        episode, evidence = normalize(request_text=f"What is in this picture? {uri} thanks")
        req = evidence[0]
        self.assertNotIn("base64,", req["content"])
        self.assertIn(MEDIA_PLACEHOLDER, req["content"])
        self.assertTrue(req["content"].endswith(" thanks"))
        cov = json.loads(episode["coverage_json"])
        self.assertTrue(cov["unsupported_media"])
        self.assertEqual(cov["redactions"], 0)

    def test_truncated_data_uri_in_tool_result_still_removed(self):
        calls = [{"tool": "Read", "tool_use_id": "tu1", "input_summary": "img.png",
                  "result_excerpt": "data:image/jpeg;base64," + "A" * 1977}]
        episode, evidence = normalize(tool_calls=calls)
        result = next(e for e in evidence if e["kind"] == "tool_result")
        self.assertEqual(result["content"], MEDIA_PLACEHOLDER)
        self.assertTrue(json.loads(episode["coverage_json"])["unsupported_media"])

    def test_harness_paths_with_secrets_are_redacted_once(self):
        skills = [{"skill": "deploy", "source": "project",
                   "path": f"/p/{self.AWS}/SKILL.md", "plugin_name": None}]
        episode, evidence = normalize(skills=skills, instructions=[])
        self.assertNotIn(self.AWS, episode["loaded_harness_json"])
        self.assertNotIn(self.AWS, "".join(e["content"] for e in evidence))
        self.assertEqual(json.loads(episode["coverage_json"])["redactions"], 1)

    def test_no_dashes_in_generated_content(self):
        episode, evidence = two_tool_env(end_reason="refusal", refusal={"category": "x"})
        text = json.dumps(episode) + json.dumps(evidence)
        self.assertIsNone(re.search("[\\u2013\\u2014]", text))


if __name__ == "__main__":
    unittest.main()
