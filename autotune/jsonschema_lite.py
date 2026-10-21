"""A small JSON Schema validator for the subset Autotune's schemas use.

Supported: type, enum, const, required, properties, additionalProperties,
items, minItems, maxItems, uniqueItems, minLength, maxLength, pattern,
minimum, maximum, $ref ("#/$defs/name"), allOf, anyOf, oneOf, if/then/else.
Unknown keywords (and format) are ignored.
"""

from __future__ import annotations

import json
import re

MAX_DEPTH = 64

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def validate(instance, schema, *, root=None) -> list[str]:
    """Return a list of error strings; empty when the instance is valid."""
    errors: list[str] = []
    _check(instance, schema, root if root is not None else schema, "", errors, 0)
    return errors


def is_valid(instance, schema, *, root=None) -> bool:
    return not validate(instance, schema, root=root)


def _where(path: str) -> str:
    return path or "/"


def _child(path: str, key) -> str:
    text = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{text}"


def _resolve(ref: str, root):
    prefix = "#/$defs/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    name = ref[len(prefix):]
    defs = root.get("$defs") if isinstance(root, dict) else None
    if not isinstance(defs, dict) or name not in defs:
        return None
    return defs[name]


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _equal(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return _canonical(a) == _canonical(b)


def _check(inst, schema, root, path, errors, depth) -> None:
    if depth > MAX_DEPTH:
        errors.append(f"{_where(path)}: schema nesting exceeds {MAX_DEPTH}")
        return
    if schema is True or schema == {}:
        return
    if schema is False:
        errors.append(f"{_where(path)}: no value is allowed here")
        return
    if not isinstance(schema, dict):
        return

    if "$ref" in schema:
        target = _resolve(schema["$ref"], root)
        if target is None:
            errors.append(f"{_where(path)}: unresolvable $ref {schema['$ref']!r}")
        else:
            _check(inst, target, root, path, errors, depth + 1)

    if "type" in schema and not _type_ok(inst, schema["type"]):
        errors.append(f"{_where(path)}: expected type {schema['type']}, got {_type_name(inst)}")
        return

    if "enum" in schema and not any(_equal(inst, v) for v in schema["enum"]):
        errors.append(f"{_where(path)}: value not in enum {schema['enum']}")
    if "const" in schema and not _equal(inst, schema["const"]):
        errors.append(f"{_where(path)}: value does not equal const {schema['const']!r}")

    if isinstance(inst, dict):
        _check_object(inst, schema, root, path, errors, depth)
    elif isinstance(inst, list):
        _check_array(inst, schema, root, path, errors, depth)
    elif isinstance(inst, str):
        _check_string(inst, schema, path, errors)
    elif _TYPE_CHECKS["number"](inst):
        _check_number(inst, schema, path, errors)

    _check_combinators(inst, schema, root, path, errors, depth)


def _type_ok(inst, declared) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    return any(_TYPE_CHECKS.get(n, lambda v: False)(inst) for n in names)


def _type_name(inst) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPE_CHECKS[name](inst):
            return name
    return type(inst).__name__


def _check_object(inst, schema, root, path, errors, depth) -> None:
    for key in schema.get("required", []):
        if key not in inst:
            errors.append(f"{_where(path)}: missing required property {key!r}")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        props = {}
    for key, value in inst.items():
        if key in props:
            _check(value, props[key], root, _child(path, key), errors, depth + 1)
        elif "additionalProperties" in schema:
            extra = schema["additionalProperties"]
            if extra is False:
                errors.append(f"{_where(path)}: additional property {key!r} is not allowed")
            elif isinstance(extra, dict):
                _check(value, extra, root, _child(path, key), errors, depth + 1)


def _check_array(inst, schema, root, path, errors, depth) -> None:
    if "minItems" in schema and len(inst) < schema["minItems"]:
        errors.append(f"{_where(path)}: expected at least {schema['minItems']} items, got {len(inst)}")
    if "maxItems" in schema and len(inst) > schema["maxItems"]:
        errors.append(f"{_where(path)}: expected at most {schema['maxItems']} items, got {len(inst)}")
    if schema.get("uniqueItems") is True:
        seen: set[str] = set()
        for i, item in enumerate(inst):
            key = _type_name(item) + ":" + _canonical(item)
            if key in seen:
                errors.append(f"{_child(path, i)}: duplicate item")
            seen.add(key)
    items = schema.get("items")
    if isinstance(items, (dict, bool)):
        for i, item in enumerate(inst):
            _check(item, items, root, _child(path, i), errors, depth + 1)


def _check_string(inst, schema, path, errors) -> None:
    if "minLength" in schema and len(inst) < schema["minLength"]:
        errors.append(f"{_where(path)}: string shorter than {schema['minLength']}")
    if "maxLength" in schema and len(inst) > schema["maxLength"]:
        errors.append(f"{_where(path)}: string longer than {schema['maxLength']}")
    if "pattern" in schema:
        try:
            matched = re.search(schema["pattern"], inst) is not None
        except re.error:
            errors.append(f"{_where(path)}: invalid pattern {schema['pattern']!r}")
            return
        if not matched:
            errors.append(f"{_where(path)}: string does not match pattern {schema['pattern']!r}")


def _check_number(inst, schema, path, errors) -> None:
    if "minimum" in schema and inst < schema["minimum"]:
        errors.append(f"{_where(path)}: {inst} is less than minimum {schema['minimum']}")
    if "maximum" in schema and inst > schema["maximum"]:
        errors.append(f"{_where(path)}: {inst} is greater than maximum {schema['maximum']}")


def _sub(inst, schema, root, path, depth) -> list[str]:
    errs: list[str] = []
    _check(inst, schema, root, path, errs, depth + 1)
    return errs


def _check_combinators(inst, schema, root, path, errors, depth) -> None:
    for sub in schema.get("allOf", []) or []:
        errors.extend(_sub(inst, sub, root, path, depth))
    if "anyOf" in schema:
        results = [_sub(inst, sub, root, path, depth) for sub in schema["anyOf"]]
        if not any(not r for r in results):
            errors.append(f"{_where(path)}: does not match any schema in anyOf")
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not _sub(inst, sub, root, path, depth))
        if matches != 1:
            errors.append(f"{_where(path)}: matches {matches} schemas in oneOf, expected exactly 1")
    if "if" in schema:
        if not _sub(inst, schema["if"], root, path, depth):
            if "then" in schema:
                errors.extend(_sub(inst, schema["then"], root, path, depth))
        elif "else" in schema:
            errors.extend(_sub(inst, schema["else"], root, path, depth))
