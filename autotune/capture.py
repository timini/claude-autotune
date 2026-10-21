"""Envelope validation and episode normalization.

parse_envelope turns raw ingress bytes into a validated dict or raises
EnvelopeError with a machine-readable code. normalize_episode turns a
validated turn_complete envelope into one episode row and its evidence rows.
Nothing here infers task success; it only records what was observed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

from .redact import redact
from .util import canonical_json, iso, new_id, parse_iso, sha256_text

MAX_ENVELOPE_BYTES = 512 * 1024
MAX_DEPTH = 32
MAX_EVIDENCE_CHARS = 4000
SCHEMA_ID = "autotune.envelope/1"

KINDS = ("turn_start", "turn_complete", "session_end")
ORIGINS = ("foreground", "worker", "judge", "author", "replay")
LIFECYCLE_SOURCES = ("startup", "resume", "clear", "compact", "fork", "unknown")
END_REASONS = ("answer", "aborted", "refusal", "error", "unknown")

MEDIA_PLACEHOLDER = "[unsupported media omitted]"
# The payload uses `*` with no end anchor so a URI cut off by the mod's length
# cap still matches.
_DATA_URI = re.compile(
    r"(?i)data:(?:image|audio|video|application|font)/[a-z0-9.+-]{1,100}"
    r"(?:;[a-z0-9.+=-]{1,100}){0,10},[A-Za-z0-9+/=%_.~-]*"
)

_SPECIAL = re.compile(r'["\\\[\]{}]')
_SESSION_ID_RAW = re.compile(rb'"session_id"\s{0,20}:\s{0,20}"([^"\\]{1,200})"')
_TURN_ID_RAW = re.compile(rb'"turn_id"\s{0,20}:\s{0,20}"([^"\\]{1,200})"')


class EnvelopeError(ValueError):
    """Invalid envelope. `code` is stable and machine-readable.

    `identity` is {"session_id", "turn_id"} when it could be recovered, so
    ingress can still record a capture error row; otherwise None.
    """

    def __init__(self, code: str, detail: str = "", field: str | None = None,
                 identity: dict | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.field = field
        self.identity = identity


# ---------------------------------------------------------------------------
# Field specifications
# ---------------------------------------------------------------------------


_COMMON = ("schema", "kind", "origin", "session_id", "cwd", "sent_at")

_TOOL_CALL_FIELDS = ("tool", "tool_use_id", "input_summary", "is_error", "result_excerpt", "denied")
_SKILL_FIELDS = ("skill", "source", "path", "plugin_name")
_INSTRUCTION_FIELDS = ("file_path", "memory_type", "load_reason")
_COVERAGE_FIELDS = ("truncated_fields", "dropped_tool_calls", "missing_turn_start", "capture_errors")

_KIND_FIELDS = {
    "turn_start": ("turn_id", "request_text", "lifecycle_id", "lifecycle_source"),
    "session_end": ("reason",),
    "turn_complete": (
        "turn_id", "lifecycle_id", "lifecycle_source", "transcript_path", "started_at",
        "completed_at", "end_reason", "refusal", "duration_ms", "usage", "model",
        "request_text", "answer_text", "tool_calls", "skills", "instructions", "coverage",
    ),
}


def _identity_from_raw(raw: bytes) -> dict | None:
    out = {}
    for key, pattern in (("session_id", _SESSION_ID_RAW), ("turn_id", _TURN_ID_RAW)):
        m = pattern.search(raw)
        if m:
            try:
                out[key] = m.group(1).decode("utf-8")
            except UnicodeDecodeError:
                pass
    if "session_id" not in out:
        return None
    out.setdefault("turn_id", None)
    return out


def _identity_from_obj(obj: Any) -> dict | None:
    if not isinstance(obj, dict):
        return None
    sid = obj.get("session_id")
    if not isinstance(sid, str) or not sid or len(sid) > 200:
        return None
    tid = obj.get("turn_id")
    if not isinstance(tid, str) or not tid or len(tid) > 200:
        tid = None
    return {"session_id": sid, "turn_id": tid}


def _check_depth(text: str) -> None:
    """Reject nesting deeper than MAX_DEPTH in one linear pass."""
    depth = 0
    in_str = False
    skip = -1
    for m in _SPECIAL.finditer(text):
        i = m.start()
        c = text[i]
        if in_str:
            if i == skip:
                continue
            if c == "\\":
                skip = i + 1
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[" or c == "{":
            depth += 1
            if depth > MAX_DEPTH:
                raise EnvelopeError("nesting_too_deep", f"nesting exceeds {MAX_DEPTH}")
        elif c == "]" or c == "}":
            depth -= 1


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


class _Validator:
    def __init__(self, obj: dict, identity: dict | None):
        self.obj = obj
        self.identity = identity

    def fail(self, field: str, detail: str, code: str = "invalid_field"):
        raise EnvelopeError(code, detail, field=field, identity=self.identity)

    def string(self, container: dict, key: str, path: str, *, required: bool, nullable: bool,
               max_len: int, min_len: int = 0, default: Any = None) -> Any:
        if key not in container:
            if required:
                self.fail(path, "missing", code="missing_field")
            return default
        value = container[key]
        if value is None:
            if nullable:
                return None
            self.fail(path, "must not be null")
        if not isinstance(value, str):
            self.fail(path, "must be a string")
        if len(value) < min_len:
            self.fail(path, "too short")
        if len(value) > max_len:
            self.fail(path, f"longer than {max_len}")
        return value

    def enum(self, container: dict, key: str, path: str, allowed: tuple, *, required: bool = True):
        value = self.string(container, key, path, required=required, nullable=False, max_len=50)
        if value is None:
            return None
        if value not in allowed:
            self.fail(path, "not an allowed value")
        return value

    def timestamp(self, container: dict, key: str, path: str, *, required: bool, nullable: bool):
        value = self.string(container, key, path, required=required, nullable=nullable, max_len=64)
        if value is None:
            return None
        try:
            parse_iso(value)
        except (ValueError, OverflowError):
            self.fail(path, "not an ISO 8601 timestamp")
        return value

    def boolean(self, container: dict, key: str, path: str, *, nullable: bool, default: Any):
        if key not in container:
            return default
        value = container[key]
        if value is None and nullable:
            return None
        if not isinstance(value, bool):
            self.fail(path, "must be a boolean")
        return value

    def integer(self, container: dict, key: str, path: str, *, nullable: bool, default: Any):
        if key not in container:
            return default
        value = container[key]
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self.fail(path, "must be a non-negative integer")
        return value

    def array(self, container: dict, key: str, path: str, max_items: int) -> list:
        if key not in container:
            self.fail(path, "missing", code="missing_field")
        value = container[key]
        if not isinstance(value, list):
            self.fail(path, "must be an array")
        if len(value) > max_items:
            self.fail(path, f"more than {max_items} items")
        return value

    def item(self, value: Any, path: str) -> dict:
        if not isinstance(value, dict):
            self.fail(path, "must be an object")
        return value


def _unknown(container: dict, allowed: tuple, prefix: str, sink: set) -> None:
    for key in container:
        if key not in allowed:
            sink.add(prefix + key)


def _validate_common(v: _Validator, obj: dict, out: dict) -> None:
    if obj.get("schema") != SCHEMA_ID:
        if "schema" not in obj:
            v.fail("schema", "missing", code="missing_field")
        v.fail("schema", "unsupported schema", code="unsupported_schema")
    out["schema"] = SCHEMA_ID
    out["kind"] = v.enum(obj, "kind", "kind", KINDS)
    out["origin"] = v.enum(obj, "origin", "origin", ORIGINS)
    out["session_id"] = v.string(obj, "session_id", "session_id", required=True, nullable=False,
                                 min_len=1, max_len=200)
    cwd = v.string(obj, "cwd", "cwd", required=True, nullable=False, min_len=1, max_len=4096)
    if not os.path.isabs(cwd):
        v.fail("cwd", "must be an absolute path")
    out["cwd"] = cwd
    out["sent_at"] = v.timestamp(obj, "sent_at", "sent_at", required=True, nullable=False)


def _validate_lifecycle(v: _Validator, obj: dict, out: dict) -> None:
    out["turn_id"] = v.string(obj, "turn_id", "turn_id", required=True, nullable=False,
                              min_len=1, max_len=200)
    out["lifecycle_id"] = v.string(obj, "lifecycle_id", "lifecycle_id", required=True,
                                   nullable=False, min_len=1, max_len=200)
    out["lifecycle_source"] = v.enum(obj, "lifecycle_source", "lifecycle_source", LIFECYCLE_SOURCES)


def _validate_turn_complete(v: _Validator, obj: dict, out: dict, unknown: set) -> None:
    _validate_lifecycle(v, obj, out)
    out["transcript_path"] = v.string(obj, "transcript_path", "transcript_path", required=False,
                                      nullable=True, max_len=4096)
    out["started_at"] = v.timestamp(obj, "started_at", "started_at", required=False, nullable=True)
    out["completed_at"] = v.timestamp(obj, "completed_at", "completed_at", required=True,
                                      nullable=False)
    out["end_reason"] = v.enum(obj, "end_reason", "end_reason", END_REASONS)

    refusal = obj.get("refusal")
    if refusal is not None:
        refusal = v.item(refusal, "refusal")
        _unknown(refusal, ("category", "explanation"), "refusal.", unknown)
        refusal = {
            "category": v.string(refusal, "category", "refusal.category", required=False,
                                 nullable=True, max_len=200),
            "explanation": v.string(refusal, "explanation", "refusal.explanation", required=False,
                                    nullable=True, max_len=8000),
        }
    out["refusal"] = refusal

    out["duration_ms"] = v.integer(obj, "duration_ms", "duration_ms", nullable=True, default=None)
    usage = obj.get("usage")
    if usage is not None and not isinstance(usage, dict):
        v.fail("usage", "must be an object or null")
    out["usage"] = usage
    out["model"] = v.string(obj, "model", "model", required=False, nullable=True, max_len=200)
    out["request_text"] = v.string(obj, "request_text", "request_text", required=False,
                                   nullable=True, max_len=32000)
    out["answer_text"] = v.string(obj, "answer_text", "answer_text", required=True,
                                  nullable=False, max_len=64000)

    calls = []
    for i, raw in enumerate(v.array(obj, "tool_calls", "tool_calls", 200)):
        p = f"tool_calls[{i}]"
        raw = v.item(raw, p)
        _unknown(raw, _TOOL_CALL_FIELDS, "tool_calls[].", unknown)
        calls.append({
            "tool": v.string(raw, "tool", p + ".tool", required=True, nullable=False,
                             min_len=1, max_len=200),
            "tool_use_id": v.string(raw, "tool_use_id", p + ".tool_use_id", required=True,
                                    nullable=False, min_len=1, max_len=200),
            "input_summary": v.string(raw, "input_summary", p + ".input_summary", required=True,
                                      nullable=False, max_len=2000),
            "is_error": v.boolean(raw, "is_error", p + ".is_error", nullable=True, default=None),
            "result_excerpt": v.string(raw, "result_excerpt", p + ".result_excerpt",
                                       required=False, nullable=True, max_len=2000),
            "denied": v.boolean(raw, "denied", p + ".denied", nullable=False, default=False),
        })
    out["tool_calls"] = calls

    skills = []
    for i, raw in enumerate(v.array(obj, "skills", "skills", 100)):
        p = f"skills[{i}]"
        raw = v.item(raw, p)
        _unknown(raw, _SKILL_FIELDS, "skills[].", unknown)
        skills.append({
            "skill": v.string(raw, "skill", p + ".skill", required=True, nullable=False,
                              min_len=1, max_len=200),
            "source": v.string(raw, "source", p + ".source", required=False, nullable=True,
                               max_len=200),
            "path": v.string(raw, "path", p + ".path", required=False, nullable=True,
                             max_len=4096),
            "plugin_name": v.string(raw, "plugin_name", p + ".plugin_name", required=False,
                                    nullable=True, max_len=200),
        })
    out["skills"] = skills

    instructions = []
    for i, raw in enumerate(v.array(obj, "instructions", "instructions", 200)):
        p = f"instructions[{i}]"
        raw = v.item(raw, p)
        _unknown(raw, _INSTRUCTION_FIELDS, "instructions[].", unknown)
        instructions.append({
            "file_path": v.string(raw, "file_path", p + ".file_path", required=True,
                                  nullable=False, min_len=1, max_len=4096),
            "memory_type": v.string(raw, "memory_type", p + ".memory_type", required=False,
                                    nullable=True, max_len=50),
            "load_reason": v.string(raw, "load_reason", p + ".load_reason", required=False,
                                    nullable=True, max_len=50),
        })
    out["instructions"] = instructions

    if "coverage" not in obj:
        v.fail("coverage", "missing", code="missing_field")
    cov = v.item(obj["coverage"], "coverage")
    _unknown(cov, _COVERAGE_FIELDS, "coverage.", unknown)
    truncated = cov.get("truncated_fields", [])
    if not isinstance(truncated, list) or len(truncated) > 100 or not all(
            isinstance(x, str) and len(x) <= 200 for x in truncated):
        v.fail("coverage.truncated_fields", "must be an array of short strings")
    errors = cov.get("capture_errors", [])
    if not isinstance(errors, list) or len(errors) > 100 or not all(
            isinstance(x, str) and len(x) <= 500 for x in errors):
        v.fail("coverage.capture_errors", "must be an array of short strings")
    out["coverage"] = {
        "truncated_fields": list(truncated),
        "dropped_tool_calls": v.integer(cov, "dropped_tool_calls", "coverage.dropped_tool_calls",
                                        nullable=False, default=0),
        "missing_turn_start": v.boolean(cov, "missing_turn_start", "coverage.missing_turn_start",
                                        nullable=False, default=False),
        "capture_errors": list(errors),
    }


def parse_envelope(raw: bytes) -> dict:
    """Validate raw ingress bytes and return a normalized envelope dict.

    Optional fields are filled with defaults. Unknown keys are ignored and
    listed (sorted, dotted paths for nested items) under `_unknown_fields`.
    Raises EnvelopeError on any violation.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise EnvelopeError("invalid_encoding", "envelope must be bytes")
    raw = bytes(raw)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError("envelope_too_large", f"{len(raw)} bytes exceeds {MAX_ENVELOPE_BYTES}",
                            identity=_identity_from_raw(raw))
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EnvelopeError("invalid_encoding", f"invalid UTF-8 at byte {exc.start}",
                            identity=_identity_from_raw(raw)) from None
    try:
        _check_depth(text)
    except EnvelopeError as exc:
        exc.identity = _identity_from_raw(raw)
        raise
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise EnvelopeError("invalid_json", str(exc)[:200], identity=_identity_from_raw(raw)) from None
    if not isinstance(obj, dict):
        raise EnvelopeError("invalid_json", "top level must be an object")
    identity = _identity_from_obj(obj)
    try:
        canonical_json(obj).encode("utf-8")
    except UnicodeEncodeError:
        raise EnvelopeError("invalid_encoding", "string contains a lone surrogate",
                            identity=None) from None

    v = _Validator(obj, identity)
    out: dict = {}
    unknown: set[str] = set()
    _validate_common(v, obj, out)
    kind = out["kind"]
    if kind == "turn_start":
        _validate_lifecycle(v, obj, out)
        out["request_text"] = v.string(obj, "request_text", "request_text", required=False,
                                       nullable=True, max_len=32000)
    elif kind == "session_end":
        out["reason"] = v.string(obj, "reason", "reason", required=True, nullable=False,
                                 max_len=200)
    else:
        _validate_turn_complete(v, obj, out, unknown)
    _unknown(obj, _COMMON + _KIND_FIELDS[kind], "", unknown)
    out["_unknown_fields"] = sorted(unknown)
    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class _Cleaner:
    """Strips data URIs, then redacts, counting both across one episode."""

    def __init__(self):
        self.redactions = 0
        self.unsupported_media = False

    def __call__(self, text: str | None) -> str | None:
        if text is None:
            return None
        text, n_media = _DATA_URI.subn(MEDIA_PLACEHOLDER, text)
        if n_media:
            self.unsupported_media = True
        text, n = redact(text)
        self.redactions += n
        return text

    def entry(self, entry: dict) -> dict:
        return {k: self(v) if isinstance(v, str) else v for k, v in entry.items()}


def _cap(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text, False
    omitted = len(text) - MAX_EVIDENCE_CHARS
    marker = f"\n[... {omitted} chars omitted ...]\n"
    room = MAX_EVIDENCE_CHARS - len(marker)
    head = room * 2 // 3
    tail = room - head
    return text[:head] + marker + text[len(text) - tail:], True


def _canonical_ts(value: str | None) -> str | None:
    return None if value is None else iso(parse_iso(value))


def normalize_episode(env: dict, repo: dict, now: datetime) -> tuple[dict, list[dict]]:
    """Build (episode_row, evidence_rows) from a validated turn_complete envelope.

    Evidence order: user_request (only when request_text is not None), every
    tool_call, every tool_result (calls with a result_excerpt), skill_load,
    instruction_load, assistant_answer, end_reason. IDs are e0001, e0002, ...
    and `position` equals the numeric part of the ID. Content is cleaned
    (data URIs replaced, secrets redacted), then capped at MAX_EVIDENCE_CHARS;
    `content_sha256` is the hash of the stored content. The input is not mutated.
    """
    if env.get("kind") != "turn_complete":
        raise EnvelopeError("invalid_field", "normalize_episode needs a turn_complete envelope",
                            field="kind")
    clean = _Cleaner()
    evidence: list[dict] = []
    truncated_ids: list[str] = []

    def add(kind: str, role: str, content: str, provenance: dict) -> None:
        position = len(evidence) + 1
        evidence_id = f"e{position:04d}"
        stored, truncated = _cap(content)
        if truncated:
            truncated_ids.append(evidence_id)
        evidence.append({
            "evidence_id": evidence_id,
            "kind": kind,
            "role": role,
            "content": stored,
            "content_sha256": sha256_text(stored),
            "provenance_json": canonical_json(provenance),
            "position": position,
            "truncated": 1 if truncated else 0,
        })

    request_text = env.get("request_text")
    if request_text is not None:
        add("user_request", "user", clean(request_text),
            {"source": "turn.complete", "field": "request_text"})

    tool_calls = env.get("tool_calls") or []
    for call in tool_calls:
        add("tool_call", "assistant", clean(call["input_summary"]), {
            "source": "tool.call",
            "tool": call["tool"],
            "tool_use_id": call["tool_use_id"],
            "denied": bool(call.get("denied", False)),
        })
    for call in tool_calls:
        if call.get("result_excerpt") is None:
            continue
        add("tool_result", "tool", clean(call["result_excerpt"]), {
            "source": "tool.result",
            "tool": call["tool"],
            "tool_use_id": call["tool_use_id"],
            "is_error": call.get("is_error"),
        })

    skills = [clean.entry(s) for s in env.get("skills") or []]
    instructions = [clean.entry(i) for i in env.get("instructions") or []]
    for index, skill in enumerate(skills):
        add("skill_load", "system", canonical_json(skill),
            {"source": "skill.load", "index": index})
    for index, instruction in enumerate(instructions):
        add("instruction_load", "system", canonical_json(instruction),
            {"source": "instructions.load", "index": index})

    add("assistant_answer", "assistant", clean(env.get("answer_text") or ""),
        {"source": "turn.complete", "field": "answer_text"})

    end_reason = env["end_reason"]
    end_content = end_reason
    refusal = env.get("refusal")
    if end_reason == "refusal":
        category = (refusal or {}).get("category") or "unknown"
        end_content = f"refusal: {clean(category)}"
        explanation = (refusal or {}).get("explanation")
        if explanation:
            end_content += "\n" + clean(explanation)
    add("end_reason", "system", end_content, {"source": "turn.complete", "field": "end_reason"})

    cov_in = env.get("coverage") or {}
    coverage = {
        "truncated_fields": list(cov_in.get("truncated_fields") or []),
        "dropped_tool_calls": int(cov_in.get("dropped_tool_calls") or 0),
        "missing_turn_start": bool(cov_in.get("missing_turn_start")) or request_text is None,
        "capture_errors": [clean(e) for e in cov_in.get("capture_errors") or []],
        "evidence_truncated": truncated_ids,
        "unknown_fields": list(env.get("_unknown_fields") or []),
        "redactions": clean.redactions,
        "unsupported_media": clean.unsupported_media,
    }

    harness = {
        "instructions": instructions,
        "skills": skills,
        "exposure": "observed" if (skills or instructions) else "unknown",
    }
    usage = env.get("usage")
    episode = {
        "episode_id": new_id("ep"),
        "session_id": env["session_id"],
        "turn_id": env["turn_id"],
        "lifecycle_id": env["lifecycle_id"],
        "lifecycle_source": env["lifecycle_source"],
        "origin": env["origin"],
        "completed_at": _canonical_ts(env["completed_at"]),
        "started_at": _canonical_ts(env.get("started_at")),
        "end_reason": end_reason,
        "duration_ms": env.get("duration_ms"),
        "model": env.get("model"),
        "transcript_path": env.get("transcript_path"),
        "repository_json": canonical_json(repo),
        "loaded_harness_json": canonical_json(harness),
        "coverage_json": canonical_json(coverage),
        "usage_json": None if usage is None else canonical_json(usage),
        "task_id": None,
        "captured_at": iso(now),
    }
    return episode, evidence
