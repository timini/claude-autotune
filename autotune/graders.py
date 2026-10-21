"""Deterministic graders run by the coordinator after a replay agent exits.

Each grader returns {"id", "type", "critical", "status", "detail"} with status
"pass", "fail" or "error". "error" means the check could not be made (spawn
failure, timeout, a path escaping the workspace, a missing fixture digest, no
answer); it makes the run ungradeable, never a pass. The model's own claims
never set a status: answers are matched as text and files are read from disk.
"""

from __future__ import annotations

import hashlib
import os
import re

from . import claude_cli

OUTCOMES = (
    "success", "task_failure", "constraint_violation", "timeout", "infrastructure_error", "invalid_output", "ungradeable",
)
DETAIL_CHARS = 300
COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")

_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_UNITTEST_RESULT = re.compile(r"^(?:OK|FAILED)(?: \(([^)\n]*)\))?\s*$", re.M)
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|xfailed|xpassed|skipped|deselected|warnings?)\b")
_PYTEST_TIME = re.compile(r"\bin [\d.]+s\b")
_PYTEST_RAN = ("passed", "failed", "error", "errors", "xfailed", "xpassed")


class _Ungradeable(Exception):
    pass


def _result(grader: dict, status: str, detail: str) -> dict:
    return {
        "id": grader["id"],
        "type": grader["type"],
        "critical": bool(grader.get("critical", True)),
        "status": status,
        "detail": detail[:DETAIL_CHARS],
    }


def grader_env(base: dict | None = None) -> dict:
    """A minimal environment for command graders: an allowlist of ordinary
    variables (so no API keys or parent session state) plus replay markers."""
    src = os.environ if base is None else base
    env = {k: src[k] for k in ENV_KEEP if k in src}
    env.update(
        AUTOTUNE_ORIGIN="replay",
        AUTOTUNE_NO_WORKER="1",
        PYTHONDONTWRITEBYTECODE="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


# Answer and tool graders


def _answer(answer: str | None) -> str:
    if answer is None:
        raise _Ungradeable("no final answer was produced")
    return answer


def _contains(grader: dict, answer: str | None) -> bool:
    text, hay = grader["text"], _answer(answer)
    if not grader.get("case_sensitive", False):
        text, hay = text.casefold(), hay.casefold()
    return text in hay


def _grade_answer_contains(grader, ctx) -> tuple[str, str]:
    if _contains(grader, ctx["answer"]):
        return "pass", f"answer contains {grader['text']!r}"
    return "fail", f"answer does not contain {grader['text']!r}"


def _grade_answer_not_contains(grader, ctx) -> tuple[str, str]:
    if _contains(grader, ctx["answer"]):
        return "fail", f"answer contains {grader['text']!r}"
    return "pass", f"answer does not contain {grader['text']!r}"


def _grade_answer_matches(grader, ctx) -> tuple[str, str]:
    try:
        found = re.search(grader["pattern"], _answer(ctx["answer"]), re.S)
    except re.error as exc:
        raise _Ungradeable(f"invalid pattern: {exc}") from None
    if found:
        return "pass", f"answer matches /{grader['pattern']}/"
    return "fail", f"answer does not match /{grader['pattern']}/"


def _tool_names(ctx) -> list[str]:
    uses = ctx["tool_uses"]
    if uses is None:
        raise _Ungradeable("tool use was not observed")
    return [u.get("name") for u in uses if isinstance(u, dict)]


def _grade_tool_used(grader, ctx) -> tuple[str, str]:
    n = _tool_names(ctx).count(grader["tool"])
    return ("pass", f"{grader['tool']} used {n} times") if n else ("fail", f"{grader['tool']} was not used")


def _grade_tool_not_used(grader, ctx) -> tuple[str, str]:
    n = _tool_names(ctx).count(grader["tool"])
    return ("fail", f"{grader['tool']} used {n} times") if n else ("pass", f"{grader['tool']} was not used")


# File graders


def _resolve(workspace: str, rel: str) -> str:
    root = os.path.realpath(workspace)
    if os.path.isabs(rel) or "\0" in rel:
        raise _Ungradeable(f"{rel!r} is not a relative path")
    full = os.path.realpath(os.path.join(root, rel))
    if full == root or not full.startswith(root + os.sep):
        raise _Ungradeable(f"{rel!r} resolves outside the workspace")
    return full


def _digest_hex(value: str) -> str:
    return value[len("sha256:"):] if value.startswith("sha256:") else value


def _grade_file_unchanged(grader, ctx) -> tuple[str, str]:
    rel = grader["path"]
    expected = (ctx["fixture_digests"] or {}).get(rel)
    if not expected:
        raise _Ungradeable(f"no fixture digest recorded for {rel!r}")
    joined = os.path.join(os.path.realpath(ctx["workspace"]), rel)
    if os.path.islink(joined):
        return "fail", f"{rel} was replaced by a symlink"
    full = _resolve(ctx["workspace"], rel)
    if not os.path.isfile(full):
        return "fail", f"{rel} no longer exists"
    with open(full, "rb") as fh:
        actual = hashlib.sha256(fh.read()).hexdigest()
    if actual == _digest_hex(expected):
        return "pass", f"{rel} unchanged"
    return "fail", f"{rel} changed"


def _grade_file_contains(grader, ctx) -> tuple[str, str]:
    rel = grader["path"]
    full = _resolve(ctx["workspace"], rel)
    if not os.path.isfile(full):
        return "fail", f"{rel} does not exist"
    with open(full, "rb") as fh:
        text = fh.read().decode("utf-8", errors="replace")
    if grader["text"] in text:
        return "pass", f"{rel} contains {grader['text']!r}"
    return "fail", f"{rel} does not contain {grader['text']!r}"


def _grade_file_absent(grader, ctx) -> tuple[str, str]:
    rel = grader["path"]
    joined = os.path.join(os.path.realpath(ctx["workspace"]), rel)
    if os.path.islink(joined):
        return "fail", f"{rel} exists (symlink)"
    full = _resolve(ctx["workspace"], rel)
    if os.path.lexists(full):
        return "fail", f"{rel} exists"
    return "pass", f"{rel} is absent"


# Command grader


def count_tests(output: str, test_format: str) -> int | None:
    """Tests actually run according to the runner's summary, or None when no
    summary was printed. Skipped tests do not count as run."""
    if test_format == "unittest":
        ran = _UNITTEST_RAN.findall(output)
        if not ran:
            return None
        total = int(ran[-1])
        tail = output[output.rfind("Ran " + ran[-1]):]
        result = _UNITTEST_RESULT.search(tail)
        skipped = 0
        if result and result.group(1):
            m = re.search(r"skipped=(\d+)", result.group(1))
            skipped = int(m.group(1)) if m else 0
        return max(0, total - skipped)
    if test_format == "pytest":
        for line in reversed(output.splitlines()):
            if not _PYTEST_TIME.search(line):
                continue
            if "no tests ran" in line:
                return 0
            counts = _PYTEST_COUNT.findall(line)
            if counts:
                return sum(int(n) for n, word in counts if word in _PYTEST_RAN)
        return None
    raise ValueError(f"unknown test_format {test_format!r}")


def _grade_command(grader, ctx) -> tuple[str, str]:
    argv = list(grader["argv"])
    timeout = float(grader.get("timeout_seconds") or 120)
    runner = ctx["runner"] or claude_cli.run_process
    outcome = runner(
        argv,
        cwd=os.path.realpath(ctx["workspace"]),
        env=grader_env(ctx["base_env"]),
        stdin_text="",
        timeout=timeout,
        cancel_event=ctx["cancel_event"],
        max_output_bytes=COMMAND_OUTPUT_BYTES,
    )
    if outcome.spawn_error:
        raise _Ungradeable(f"could not start {argv[0]!r}: {outcome.spawn_error}")
    if outcome.timed_out:
        raise _Ungradeable(f"command exceeded {timeout:g}s")
    if outcome.cancelled:
        raise _Ungradeable("command cancelled")
    expect = int(grader.get("expect_exit", 0))
    if outcome.exit_code != expect:
        return "fail", f"exit {outcome.exit_code}, expected {expect}: {_tail(outcome)}"
    test_format = grader.get("test_format")
    if not test_format:
        return "pass", f"exit {outcome.exit_code}"
    min_tests = grader.get("min_tests") or 1
    count = count_tests(outcome.stdout + "\n" + outcome.stderr, test_format)
    if count is None:
        if outcome.output_truncated:
            raise _Ungradeable("output truncated before the test summary")
        return "fail", f"exit {outcome.exit_code} but no {test_format} test count was printed"
    if count < min_tests:
        return "fail", f"{count} tests ran, expected at least {min_tests}"
    return "pass", f"exit {outcome.exit_code}, {count} tests ran"


def _tail(outcome) -> str:
    text = (outcome.stderr or "").strip() or (outcome.stdout or "").strip()
    return text[-160:]


_GRADERS = {
    "answer_contains": _grade_answer_contains,
    "answer_not_contains": _grade_answer_not_contains,
    "answer_matches": _grade_answer_matches,
    "file_unchanged": _grade_file_unchanged,
    "file_contains": _grade_file_contains,
    "file_absent": _grade_file_absent,
    "tool_used": _grade_tool_used,
    "tool_not_used": _grade_tool_not_used,
    "command": _grade_command,
}


def run_grader(grader: dict, ctx: dict) -> dict:
    fn = _GRADERS.get(grader.get("type"))
    if fn is None:
        return _result(grader, "error", f"unknown grader type {grader.get('type')!r}")
    try:
        status, detail = fn(grader, ctx)
    except _Ungradeable as exc:
        return _result(grader, "error", str(exc))
    except Exception as exc:  # a grader bug or I/O error must not pass silently
        return _result(grader, "error", f"{type(exc).__name__}: {exc}")
    return _result(grader, status, detail)


def run_graders(
    case,
    workspace: str,
    *,
    answer: str | None,
    tool_uses: list[dict] | None,
    fixture_digests: dict[str, str] | None,
    runner=None,
    base_env: dict | None = None,
    cancel_event=None,
) -> list[dict]:
    """Grade one run. File, answer and tool checks run before command graders
    (a command may change files); results come back in declared order."""
    graders = case.graders if hasattr(case, "graders") else case["graders"]
    ctx = {
        "workspace": str(workspace),
        "answer": answer,
        "tool_uses": tool_uses,
        "fixture_digests": fixture_digests,
        "runner": runner,
        "base_env": base_env,
        "cancel_event": cancel_event,
    }
    results: dict[int, dict] = {}
    order = sorted(range(len(graders)), key=lambda i: graders[i].get("type") == "command")
    for i in order:
        results[i] = run_grader(graders[i], ctx)
    return [results[i] for i in range(len(graders))]


def outcome_for(results: list[dict], error_code: str | None = None) -> str:
    """Run outcome. An arm error wins; then a failed critical grader
    (constraint_violation, definite evidence even if another grader errored);
    then any grader error (ungradeable); then a failed grader (task_failure)."""
    if error_code:
        if error_code == "timeout":
            return "timeout"
        if error_code == "invalid_output":
            return "invalid_output"
        return "infrastructure_error"
    if not results:
        return "ungradeable"
    if any(r["status"] == "fail" and r.get("critical") for r in results):
        return "constraint_violation"
    if any(r["status"] == "error" for r in results):
        return "ungradeable"
    if any(r["status"] == "fail" for r in results):
        return "task_failure"
    return "success"


def failed_ids(results: list[dict]) -> list[str]:
    return [r["id"] for r in results if r["status"] != "pass"]
