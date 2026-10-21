"""Keeps schemas/envelope.schema.json in step with capture.parse_envelope."""

import copy
import json
import unittest
from pathlib import Path

from autotune import capture
from autotune.capture import EnvelopeError, parse_envelope

from tests.test_capture_parse import encode, turn_complete

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "envelope.schema.json").read_text())


def _base_for(key_path):
    env = turn_complete(refusal={"category": "c", "explanation": "e"}, end_reason="refusal")
    if key_path[0] == "reason":
        env = {k: env[k] for k in capture._COMMON}
        env.update(kind="session_end", reason="other")
    return env


def _set(env, key_path, value):
    target = env
    for part in key_path[:-1]:
        target = target[part][0] if isinstance(target[part], list) else target[part]
    target[key_path[-1]] = value


def _string_props(props, prefix=()):
    """Yield (key_path, spec) for every property in the schema, descending into objects and items."""
    for name, spec in props.items():
        path = prefix + (name,)
        yield path, spec
        if "properties" in spec:
            yield from _string_props(spec["properties"], path)
        if "items" in spec and "properties" in spec["items"]:
            yield from _string_props(spec["items"]["properties"], path)


def _types(spec):
    t = spec.get("type", [])
    return {t} if isinstance(t, str) else set(t)


class EnvelopeSchemaDriftTest(unittest.TestCase):
    def test_enums_match_capture_constants(self):
        props = SCHEMA["properties"]
        self.assertEqual(props["schema"]["const"], capture.SCHEMA_ID)
        self.assertEqual(tuple(props["kind"]["enum"]), capture.KINDS)
        self.assertEqual(tuple(props["origin"]["enum"]), capture.ORIGINS)
        self.assertEqual(tuple(props["lifecycle_source"]["enum"]), capture.LIFECYCLE_SOURCES)
        self.assertEqual(tuple(props["end_reason"]["enum"]), capture.END_REASONS)

    def test_required_fields_match(self):
        self.assertEqual(tuple(SCHEMA["required"]), capture._COMMON)
        for rule in SCHEMA["allOf"]:
            kind = rule["if"]["properties"]["kind"]["const"]
            for field in rule["then"]["required"]:
                self.assertIn(field, capture._KIND_FIELDS[kind])
        props = set(SCHEMA["properties"])
        for kind, fields in capture._KIND_FIELDS.items():
            self.assertLessEqual(set(fields), props, kind)

    def test_every_free_string_has_a_max_length(self):
        for path, spec in _string_props(SCHEMA["properties"]):
            if "string" in _types(spec) and "format" not in spec and "enum" not in spec:
                self.assertIn("maxLength", spec, ".".join(path))
            if spec.get("type") == "array" and "string" in _types(spec.get("items", {})):
                self.assertIn("maxLength", spec["items"], ".".join(path))

    def test_max_lengths_are_the_limits_ingress_enforces(self):
        for path, spec in _string_props(SCHEMA["properties"]):
            if "maxLength" not in spec or "enum" in spec:
                continue
            limit = spec["maxLength"]
            with self.subTest(field=".".join(path)):
                ok = _base_for(path)
                _set(ok, path, "/" + "x" * (limit - 1))
                parse_envelope(encode(ok))
                bad = _base_for(path)
                _set(bad, path, "/" + "x" * limit)
                with self.assertRaises(EnvelopeError):
                    parse_envelope(encode(bad))

    def test_max_items_are_the_limits_ingress_enforces(self):
        for path, spec in _string_props(SCHEMA["properties"]):
            if spec.get("type") != "array" or "maxItems" not in spec:
                continue
            limit = spec["maxItems"]
            with self.subTest(field=".".join(path)):
                env = _base_for(path)
                parent = env if len(path) == 1 else env[path[0]]
                current = parent[path[-1]]
                sample = current[0] if current else "x"
                items = []
                for i in range(limit + 1):
                    item = copy.deepcopy(sample)
                    if isinstance(item, dict):
                        first = next(iter(item))
                        item[first] = f"{item[first]}-{i}"
                    items.append(item)
                parent[path[-1]] = items[:limit]
                parse_envelope(encode(env))
                parent[path[-1]] = items
                with self.assertRaises(EnvelopeError):
                    parse_envelope(encode(env))
