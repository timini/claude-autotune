from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from autotune.jsonschema_lite import MAX_DEPTH, validate

ROOT = Path(__file__).resolve().parent.parent


def _load(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


class ReviewSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _load("schemas/review.schema.json")
        self.review = _load("examples/review.json")["review"]

    def test_example_review_is_valid(self):
        self.assertEqual(validate(self.review, self.schema), [])

    def _errors_after(self, mutate) -> list[str]:
        obj = copy.deepcopy(self.review)
        mutate(obj)
        return validate(obj, self.schema)

    def test_each_deliberate_violation_is_reported(self):
        def drop_required(o):
            del o["recommendation"]

        def bad_enum(o):
            o["task_stage"] = "done"

        def extra_property(o):
            o["verdict"] = "great"

        def score_above_maximum(o):
            o["dimensions"]["outcome"]["score"] = 5

        def score_below_minimum(o):
            o["dimensions"]["outcome"]["score"] = -1

        def score_is_bool(o):
            o["dimensions"]["verification"]["score"] = True

        def score_is_float(o):
            o["dimensions"]["verification"]["score"] = 2.5

        def bad_evidence_pattern(o):
            o["dimensions"]["outcome"]["evidence_ids"] = ["x1"]

        def duplicate_evidence(o):
            o["dimensions"]["outcome"]["evidence_ids"] = ["e0001", "e0001"]

        def empty_reason(o):
            o["dimensions"]["communication"]["reason"] = ""

        def reason_too_long(o):
            o["dimensions"]["communication"]["reason"] = "x" * 1201

        def scored_without_evidence(o):
            o["dimensions"]["outcome"]["evidence_ids"] = []

        def finding_without_evidence(o):
            o["improvements"][0]["evidence_ids"] = []

        def too_many_strengths(o):
            o["strengths"] = [copy.deepcopy(o["strengths"][0]) for _ in range(9)]

        def wrong_type_for_dimensions(o):
            o["dimensions"] = []

        def missing_dimension(o):
            del o["dimensions"]["efficiency"]

        def bad_basis(o):
            o["strengths"][0]["basis"] = "guessed"

        cases = [
            ("/: missing required property 'recommendation'", drop_required),
            ("/task_stage", bad_enum),
            ("additional property 'verdict'", extra_property),
            ("/dimensions/outcome/score: 5 is greater than maximum", score_above_maximum),
            ("/dimensions/outcome/score: -1 is less than minimum", score_below_minimum),
            ("/dimensions/verification/score: expected type", score_is_bool),
            ("/dimensions/verification/score: expected type", score_is_float),
            ("/dimensions/outcome/evidence_ids/0: string does not match pattern", bad_evidence_pattern),
            ("/dimensions/outcome/evidence_ids/1: duplicate item", duplicate_evidence),
            ("/dimensions/communication/reason: string shorter than 1", empty_reason),
            ("/dimensions/communication/reason: string longer than 1200", reason_too_long),
            ("/dimensions/outcome/evidence_ids: expected at least 1 items", scored_without_evidence),
            ("/improvements/0/evidence_ids: expected at least 1 items", finding_without_evidence),
            ("/strengths: expected at most 8 items", too_many_strengths),
            ("/dimensions: expected type object, got array", wrong_type_for_dimensions),
            ("/dimensions: missing required property 'efficiency'", missing_dimension),
            ("/strengths/0/basis: value not in enum", bad_basis),
        ]
        for expected, mutate in cases:
            with self.subTest(mutate.__name__):
                errors = self._errors_after(mutate)
                self.assertTrue(any(expected in e for e in errors), f"{expected!r} not in {errors}")

    def test_null_score_needs_no_evidence(self):
        errors = self._errors_after(lambda o: o["dimensions"]["outcome"].update(score=None, evidence_ids=[]))
        self.assertEqual(errors, [])


class KeywordTests(unittest.TestCase):
    def test_integer_and_number_exclude_bool(self):
        self.assertEqual(validate(3, {"type": "integer"}), [])
        self.assertNotEqual(validate(True, {"type": "integer"}), [])
        self.assertNotEqual(validate(False, {"type": "number"}), [])
        self.assertNotEqual(validate(1.0, {"type": "integer"}), [])
        self.assertEqual(validate(1.5, {"type": "number"}), [])

    def test_enum_and_const_distinguish_bool_from_int(self):
        self.assertNotEqual(validate(True, {"enum": [1, 2]}), [])
        self.assertNotEqual(validate(1, {"const": True}), [])
        self.assertEqual(validate({"a": [1]}, {"const": {"a": [1]}}), [])

    def test_max_length_counts_code_points(self):
        self.assertEqual(validate("\U0001f600" * 3, {"maxLength": 3}), [])
        self.assertNotEqual(validate("abcd", {"maxLength": 3}), [])

    def test_pattern_is_a_search(self):
        self.assertEqual(validate("xxabc", {"pattern": "abc"}), [])
        self.assertNotEqual(validate("xxabc", {"pattern": "^abc"}), [])

    def test_additional_properties_schema(self):
        schema = {"properties": {"a": {}}, "additionalProperties": {"type": "string"}}
        self.assertEqual(validate({"a": 1, "b": "x"}, schema), [])
        self.assertIn("/b: expected type string, got integer", validate({"b": 2}, schema))

    def test_any_of_one_of(self):
        any_of = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(validate(1, any_of), [])
        self.assertNotEqual(validate([], any_of), [])
        one_of = {"oneOf": [{"type": "integer"}, {"minimum": 0}]}
        self.assertEqual(validate(-1, one_of), [])
        self.assertTrue(any("oneOf" in e for e in validate(1, one_of)))

    def test_if_then_else(self):
        schema = {"if": {"type": "string"}, "then": {"minLength": 2}, "else": {"type": "integer"}}
        self.assertEqual(validate("ab", schema), [])
        self.assertNotEqual(validate("a", schema), [])
        self.assertEqual(validate(3, schema), [])
        self.assertNotEqual(validate(None, schema), [])

    def test_unresolvable_ref_is_an_error(self):
        errors = validate(1, {"$ref": "#/$defs/missing"})
        self.assertTrue(any("unresolvable $ref" in e for e in errors))
        errors = validate(1, {"$ref": "http://example.com/s.json"})
        self.assertTrue(any("unresolvable $ref" in e for e in errors))

    def test_unknown_keywords_and_format_ignored(self):
        self.assertEqual(validate("not-a-date", {"format": "date-time", "x-custom": 1}), [])

    def test_recursive_ref_is_bounded(self):
        schema = {"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}}
        errors = validate(1, schema)
        self.assertTrue(any(f"exceeds {MAX_DEPTH}" in e for e in errors))

    def test_deep_instance_is_bounded(self):
        schema = {"$defs": {"node": {"type": "array", "items": {"$ref": "#/$defs/node"}}}, "$ref": "#/$defs/node"}
        value: list = []
        for _ in range(200):
            value = [value]
        errors = validate(value, schema)
        self.assertTrue(any("exceeds" in e for e in errors))

    def test_explicit_root_for_subschema(self):
        root = {"$defs": {"id": {"type": "string"}}}
        self.assertEqual(validate("a", {"$ref": "#/$defs/id"}, root=root), [])
        self.assertNotEqual(validate(1, {"$ref": "#/$defs/id"}, root=root), [])

    def test_pointer_escapes_keys(self):
        errors = validate({"a/b": 1}, {"properties": {"a/b": {"type": "string"}}})
        self.assertTrue(errors[0].startswith("/a~1b:"))


if __name__ == "__main__":
    unittest.main()
