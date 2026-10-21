#!/usr/bin/env python3
"""A scripted stand-in for the `claude` CLI, for tests. Never calls a model.

Environment:
  AUTOTUNE_FAKE_AUTH    raw JSON printed by `auth status --json` (default: logged in via claude.ai)
  AUTOTUNE_FAKE_LOG     JSON lines file; one line per `-p` call
  AUTOTUNE_FAKE_SCRIPT  JSON file {"responses": [...]}; `<script>.idx` holds the position

Response keys (all optional): role, structured_output, result, model, exit_code,
stdout_raw, stderr, sleep, is_error, subtype, api_error_status, edits, chmod,
symlinks, spawn, spawn_background, spawn_background_pid_file (where the fake
writes the background child's pid), usage, cost_usd, session_id, permission_denials,
tool_uses, when_file_contains, when_file_not_contains.

Conditional responses: a response with "when_file_contains" or
"when_file_not_contains" ({"path": relative to cwd, "text": ...}) is chosen,
first match in list order and without moving the counter, when its role and
condition match the workspace at call time. The other responses are taken in
order by the counter. So a test can make the answer depend on the harness
(for example the CLAUDE.md) materialized in a replay workspace.

With `--output-format stream-json` (which, like the real CLI, requires
`--verbose`) the fake prints one JSON object per line: a system init event,
an assistant tool_use message and a user tool_result message for each entry of
"tool_uses" ([{"name", "input", "result", "is_error"}]), an assistant text
message, and last the same result object as json mode.

`--version` prints $AUTOTUNE_FAKE_VERSION or "0.0.0-fake (Claude Code)".
The log line also carries "claude_md": the cwd CLAUDE.md text or null.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid

DEFAULT_AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
DEFAULT_VERSION = "0.0.0-fake (Claude Code)"
CLAUDE_MD_LOG_CHARS = 20000
DEFAULT_USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _auth_status() -> int:
    raw = os.environ.get("AUTOTUNE_FAKE_AUTH")
    sys.stdout.write(raw if raw is not None else json.dumps(DEFAULT_AUTH))
    sys.stdout.write("\n")
    return 0


def _log(argv: list[str], stdin: str) -> None:
    path = os.environ.get("AUTOTUNE_FAKE_LOG")
    if not path:
        return
    env = {
        "AUTOTUNE_ORIGIN": os.environ.get("AUTOTUNE_ORIGIN"),
        "AUTOTUNE_NO_WORKER": os.environ.get("AUTOTUNE_NO_WORKER"),
        "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY" in os.environ,
        "CLAUDECODE": "CLAUDECODE" in os.environ,
    }
    line = json.dumps({
        "argv": argv, "cwd": os.getcwd(), "stdin": stdin, "env": env, "pid": os.getpid(),
        "claude_md": _read_cwd_file("CLAUDE.md", CLAUDE_MD_LOG_CHARS),
    })
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line + "\n")


def _read_cwd_file(rel: str, limit: int | None = None) -> str | None:
    try:
        path = _inside(rel)
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read() if limit is None else fh.read(limit)
    except (OSError, ValueError):
        return None


def _matches(response: dict, origin: str | None) -> bool:
    return "role" not in response or response["role"] == origin


def _conditional(response: dict) -> bool:
    return "when_file_contains" in response or "when_file_not_contains" in response


def _condition_holds(response: dict) -> bool:
    for key, want in (("when_file_contains", True), ("when_file_not_contains", False)):
        cond = response.get(key)
        if cond is None:
            continue
        text = _read_cwd_file(cond["path"])
        found = text is not None and cond["text"] in text
        if found != want:
            return False
    return True


def _next_response() -> dict:
    script = os.environ.get("AUTOTUNE_FAKE_SCRIPT")
    if not script:
        return {}
    with open(script, encoding="utf-8") as fh:
        responses = json.load(fh).get("responses", [])
    origin = os.environ.get("AUTOTUNE_ORIGIN")
    for response in responses:
        if _conditional(response) and _matches(response, origin) and _condition_holds(response):
            return response
    responses = [r for r in responses if not _conditional(r)]
    with open(script + ".idx", "a+", encoding="utf-8") as idx:
        fcntl.flock(idx, fcntl.LOCK_EX)
        idx.seek(0)
        text = idx.read().strip()
        pos = int(text) if text.isdigit() else 0
        chosen = None
        for i in range(pos, len(responses)):
            if _matches(responses[i], origin):
                chosen, pos = responses[i], i + 1
                break
        if chosen is None:
            matching = [r for r in responses if _matches(r, origin)]
            chosen = matching[-1] if matching else {}
        idx.seek(0)
        idx.truncate()
        idx.write(str(pos))
    return chosen


def _episode_id(stdin: str) -> str | None:
    lines = stdin.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "DATA" and i + 1 < len(lines):
            try:
                data = json.loads(lines[i + 1])
            except ValueError:
                break
            if isinstance(data, dict) and isinstance(data.get("episode_id"), str):
                return data["episode_id"]
    m = re.search(r'"episode_id"\s*:\s*"([^"\\]+)"', stdin)
    return m.group(1) if m else None


def _echo(value, episode_id):
    if value == "$echo_episode":
        return episode_id
    if isinstance(value, dict):
        return {k: _echo(v, episode_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_echo(v, episode_id) for v in value]
    return value


def _inside(rel: str) -> str:
    root = os.path.realpath(os.getcwd())
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.isabs(rel) or not (target == root or target.startswith(root + os.sep)):
        raise ValueError(f"path escapes cwd: {rel}")
    return os.path.join(root, rel)


def _apply_files(response: dict) -> None:
    for rel, content in (response.get("edits") or {}).items():
        path = _inside(rel)
        if content is None:
            if os.path.lexists(path):
                os.unlink(path)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    for rel, mode in (response.get("chmod") or {}).items():
        os.chmod(_inside(rel), int(mode, 8) if isinstance(mode, str) else mode)
    for rel, target in (response.get("symlinks") or {}).items():
        link = os.path.join(os.path.realpath(os.getcwd()), rel)
        _inside(os.path.dirname(rel) or ".")
        if os.path.lexists(link):
            os.unlink(link)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)


def _result_object(response: dict, stdin: str, started: float) -> dict:
    model = response.get("model", "claude-fake-1")
    usage = response.get("usage") or dict(DEFAULT_USAGE)
    cost = response.get("cost_usd", 0.001)
    is_error = bool(response.get("is_error", False))
    obj = {
        "type": "result",
        "subtype": response.get("subtype", "error_during_execution" if is_error else "success"),
        "is_error": is_error,
        "result": response.get("result", ""),
        "session_id": response.get("session_id", str(uuid.uuid4())),
        "total_cost_usd": cost,
        "usage": usage,
        "modelUsage": {
            model: {
                "inputTokens": usage.get("input_tokens", 0),
                "outputTokens": usage.get("output_tokens", 0),
                "costUSD": cost,
                "canonicalModel": model,
            }
        },
        "permission_denials": response.get("permission_denials", []),
        "terminal_reason": "completed",
        "stop_reason": "tool_use",
        "num_turns": 1,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "api_error_status": response.get("api_error_status"),
    }
    if "structured_output" in response:
        value = response["structured_output"]
        episode_id = _episode_id(stdin)
        if value == "$echo_episode":
            value = {"episode_id": episode_id}
        obj["structured_output"] = _echo(value, episode_id)
    return obj


def _stream_lines(response: dict, result: dict) -> list[dict]:
    session = result["session_id"]
    model = response.get("model", "claude-fake-1")
    lines = [{"type": "system", "subtype": "init", "session_id": session, "model": model, "cwd": os.getcwd()}]
    for n, use in enumerate(response.get("tool_uses") or []):
        use_id = f"toolu_fake_{n}"
        lines.append({
            "type": "assistant",
            "session_id": session,
            "message": {"role": "assistant", "model": model, "content": [
                {"type": "tool_use", "id": use_id, "name": use["name"], "input": use.get("input", {})},
            ]},
        })
        lines.append({
            "type": "user",
            "session_id": session,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": use_id, "content": use.get("result", ""),
                 "is_error": bool(use.get("is_error", False))},
            ]},
        })
    lines.append({
        "type": "assistant",
        "session_id": session,
        "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": result["result"]}]},
    })
    lines.append(result)
    return lines


def _output_format(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "--output-format" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--output-format="):
            return arg.split("=", 1)[1]
    return "text"


def _print_mode(argv: list[str]) -> int:
    started = time.monotonic()
    stdin = sys.stdin.read()
    _log(argv, stdin)
    fmt = _output_format(argv)
    if fmt == "stream-json" and "--verbose" not in argv:
        sys.stderr.write("Error: When using --print, --output-format=stream-json requires --verbose\n")
        return 1
    response = _next_response()
    try:
        _apply_files(response)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"fake_claude: {exc}\n")
        return 2
    if response.get("spawn"):
        subprocess.run(response["spawn"], env=os.environ.copy(), check=False)
    if response.get("spawn_background"):
        child = subprocess.Popen(
            response["spawn_background"],
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if response.get("spawn_background_pid_file"):
            target = response["spawn_background_pid_file"]
            with open(target + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(str(child.pid))
            os.replace(target + ".tmp", target)
    if response.get("sleep"):
        time.sleep(float(response["sleep"]))
    if response.get("stderr"):
        sys.stderr.write(response["stderr"])
    if "stdout_raw" in response:
        sys.stdout.write(response["stdout_raw"])
    elif fmt == "stream-json":
        for line in _stream_lines(response, _result_object(response, stdin, started)):
            sys.stdout.write(json.dumps(line) + "\n")
    else:
        sys.stdout.write(json.dumps(_result_object(response, stdin, started)) + "\n")
    sys.stdout.flush()
    return int(response.get("exit_code", 0))


def main(argv: list[str]) -> int:
    if argv[:3] == ["auth", "status", "--json"]:
        return _auth_status()
    if argv[:1] in (["--version"], ["-v"]):
        sys.stdout.write((os.environ.get("AUTOTUNE_FAKE_VERSION") or DEFAULT_VERSION) + "\n")
        return 0
    if "-p" in argv or "--print" in argv:
        return _print_mode(argv)
    sys.stderr.write(f"fake_claude: unsupported arguments {argv!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
