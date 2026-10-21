"""Adapter that runs `claude -p` as a child process for review and experiment calls.

Every call: check auth, reserve a budget slot, run the process with no DB
transaction open, then settle the reservation. Argv is always a list; the
prompt goes on stdin. Never --bare and never --dangerously-skip-permissions.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import budget
from .util import canonical_json, new_id, utc_now

ROLE_PROFILES = {"review": "reviewer", "judge": "reviewer", "curator": "reviewer", "author": "author", "replay": "replay"}
ROLE_ORIGINS = {"review": "worker", "judge": "judge", "curator": "worker", "author": "author", "replay": "replay"}
AUTHOR_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
AUTHOR_FORBIDDEN_TOOLS = frozenset({"Bash", "WebFetch", "WebSearch", "Task", "Agent"})
PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
)
PARENT_SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
PARENT_SESSION_PREFIXES = ("CLAUDE_CODE_SSE",)
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
POLL_SECONDS = 0.05
KILL_GRACE_SECONDS = 2.0
AUTH_TIMEOUT_SECONDS = 20.0
AUTH_CACHE = timedelta(minutes=10)
DETAIL_CHARS = 300
TOOL_INPUT_SUMMARY_CHARS = 300
MAX_TOOL_USES = 500
_PATTERN_FORBIDDEN = ("\n", "\r", "\x00")

# Every error code this module can put in CallResult.error_code.
ERROR_CODES = (
    "auth_required",
    "budget_exhausted",
    "cancelled",
    "cli_incompatible",
    "cli_missing",
    "environment",
    "invalid_output",
    "overloaded",
    "rate_limited",
    "server_error",
    "timeout",
    "transient",
    "usage_limit",
)


@dataclass
class CallSpec:
    role: str
    prompt: str
    system_prompt: str | None
    json_schema: dict | None
    cwd: str
    model: str | None
    timeout_seconds: float
    tools: list[str]
    profile: str
    add_dirs: list[str] = field(default_factory=list)
    max_budget_usd: float | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    # Replay profile only: permission allow rules (one --allowedTools each) and
    # a --settings object.
    allowed_tools_patterns: list[str] = field(default_factory=list)
    settings: dict | None = None


@dataclass
class CallResult:
    ok: bool
    error_code: str | None
    detail: str | None
    structured: dict | None
    text: str | None
    model: str | None
    usage: dict | None
    cost_usd: float | None
    duration_ms: int
    cli_session_id: str | None
    permission_denials: list
    exit_code: int | None
    call_id: str
    # Replay (stream-json) only: [{"name", "input_summary", "is_error"}] in call order.
    tool_uses: list = field(default_factory=list)


@dataclass
class ProcOutcome:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int = 0
    spawn_error: str | None = None
    output_truncated: bool = False


# Argv and environment


def _author_tools(tools: list[str]) -> list[str]:
    chosen = list(tools) if tools else list(AUTHOR_TOOLS)
    return [t for t in chosen if t and t not in AUTHOR_FORBIDDEN_TOOLS]


def output_format(spec: CallSpec) -> str:
    """Replay uses stream-json so tool calls are observable; every other profile uses json."""
    return "stream-json" if spec.profile == "replay" else "json"


def _check_pattern(pattern: object) -> str:
    if not isinstance(pattern, str) or not pattern.strip() or any(c in pattern for c in _PATTERN_FORBIDDEN):
        raise ValueError(f"invalid allowed tool pattern {pattern!r}")
    return pattern


def build_argv(executable: str, spec: CallSpec) -> list[str]:
    if spec.profile != "replay" and (spec.allowed_tools_patterns or spec.settings is not None):
        raise ValueError("allowed_tools_patterns and settings are only supported by the replay profile")
    fmt = output_format(spec)
    argv = [executable, "-p", "--output-format", fmt, "--no-session-persistence"]
    if fmt == "stream-json":
        argv.append("--verbose")
    if spec.profile == "reviewer":
        argv += ["--safe-mode", "--tools", "", "--strict-mcp-config"]
    elif spec.profile == "author":
        argv += [
            "--safe-mode",
            "--restricted",
            "--tools",
            ",".join(_author_tools(spec.tools)),
            "--permission-mode",
            "acceptEdits",
            "--strict-mcp-config",
        ]
    elif spec.profile == "replay":
        argv += [
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(t for t in spec.tools if t),
        ]
        for d in spec.add_dirs:
            argv += ["--add-dir", str(d)]
        if spec.allowed_tools_patterns:
            # One flag followed by every rule, the documented form; the next
            # argv element is always another flag since the prompt is on stdin.
            argv += ["--allowedTools", *(_check_pattern(p) for p in spec.allowed_tools_patterns)]
        if spec.settings is not None:
            argv += ["--settings", canonical_json(spec.settings)]
    else:
        raise ValueError(f"unknown call profile {spec.profile!r}")
    if spec.model:
        argv += ["--model", spec.model]
    if spec.system_prompt is not None:
        argv += ["--system-prompt", spec.system_prompt]
    if spec.json_schema is not None:
        argv += ["--json-schema", canonical_json(spec.json_schema)]
    if spec.max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{float(spec.max_budget_usd):g}"]
    return argv


def _strip_parent_session(key: str) -> bool:
    return key in PARENT_SESSION_ENV or key.startswith(PARENT_SESSION_PREFIXES)


def child_env(base_env: dict, spec: CallSpec, cfg: dict) -> dict:
    env = dict(base_env)
    if not cfg.get("allow_api_key", False):
        for key in PROVIDER_ENV:
            env.pop(key, None)
    for key in [k for k in env if _strip_parent_session(k)]:
        del env[key]
    env["AUTOTUNE_ORIGIN"] = ROLE_ORIGINS.get(spec.role, "worker")
    env["AUTOTUNE_NO_WORKER"] = "1"
    env.update(spec.extra_env)
    return env


# Process runner


def _reader(stream, sink: list, cap: int, flags: dict) -> None:
    size = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            keep = chunk[: max(0, cap - size)]
            if keep:
                sink.append(keep)
                size += len(keep)
            if len(keep) < len(chunk):
                flags["truncated"] = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _writer(stream, data: bytes) -> None:
    try:
        stream.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _spawn_error_code(exc: OSError) -> str:
    if isinstance(exc, PermissionError) or exc.errno == errno.ENOEXEC:
        return "cli_incompatible"
    return "cli_missing"


def run_process(
    argv: list[str],
    *,
    cwd: str,
    env: dict,
    stdin_text: str,
    timeout: float,
    cancel_event: threading.Event | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> ProcOutcome:
    started = time.monotonic()
    if not os.path.isdir(cwd):
        return ProcOutcome(None, "", "", spawn_error="environment", duration_ms=0)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return ProcOutcome(None, "", "", spawn_error=_spawn_error_code(exc), duration_ms=0)

    out: list[bytes] = []
    err: list[bytes] = []
    flags: dict = {}
    threads = [
        threading.Thread(target=_writer, args=(proc.stdin, stdin_text.encode("utf-8")), daemon=True),
        threading.Thread(target=_reader, args=(proc.stdout, out, max_output_bytes, flags), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err, max_output_bytes, {}), daemon=True),
    ]
    timed_out = cancelled = False
    stop_group = True
    try:
        for t in threads:
            t.start()
        deadline = started + timeout
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(POLL_SECONDS)
        stop_group = timed_out or cancelled
    finally:
        # Runs on every exit, including an exception or a signal raised in the
        # poll loop: the child has its own session, so nothing else stops it.
        if stop_group:
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        # Leftover members of the call's process group (grandchildren) never outlive it.
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
    for t in threads:
        if t.ident is not None:
            t.join(timeout=KILL_GRACE_SECONDS)

    return ProcOutcome(
        exit_code=proc.returncode,
        stdout=b"".join(out).decode("utf-8", errors="replace"),
        stderr=b"".join(err).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=int((time.monotonic() - started) * 1000),
        output_truncated=bool(flags.get("truncated")),
    )


# Output parsing and classification


def parse_output(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    candidates = [text] + [ln for ln in reversed(text.splitlines()) if ln.strip()][:1]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _summary(value) -> str:
    try:
        text = value if isinstance(value, str) else canonical_json(value)
    except (TypeError, ValueError):
        text = repr(value)
    return text[:TOOL_INPUT_SUMMARY_CHARS]


def parse_stream(stdout: str) -> tuple[dict | None, list[dict]]:
    """Parse `--output-format stream-json --verbose` output: one JSON object per
    line. Returns the last `{"type": "result"}` object (None when absent) and the
    tool_use blocks of assistant messages in order, each marked with the
    is_error flag of its matching tool_result when one was seen."""
    result = None
    uses: list[dict] = []
    by_id: dict[str, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "result":
            result = event
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use" and len(uses) < MAX_TOOL_USES:
                use = {
                    "name": str(block.get("name") or ""),
                    "input_summary": _summary(block.get("input", {})),
                    "is_error": None,
                }
                uses.append(use)
                if isinstance(block.get("id"), str):
                    by_id[block["id"]] = use
            elif kind == "user" and block.get("type") == "tool_result":
                use = by_id.get(block.get("tool_use_id"))
                if use is not None:
                    use["is_error"] = bool(block.get("is_error", False))
    return result, uses


def _status_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def classify_text(text: str, status: int | None = None) -> str | None:
    low = (text or "").lower()
    # Explicit usage wording wins; a transient rate limit ("Rate limit reached")
    # must not be read as the daily usage limit, so the generic phrase comes last.
    if "usage limit" in low or "quota" in low:
        return "usage_limit"
    if status in (401, 403) or "not logged in" in low or "login" in low or "authenticat" in low:
        return "auth_required"
    if status == 429 or "rate limit" in low:
        return "rate_limited"
    if "limit reached" in low:
        return "usage_limit"
    if status == 529 or "overloaded" in low:
        return "overloaded"
    if status is not None and 500 <= status <= 599:
        return "server_error"
    if "unknown option" in low or "unrecognized option" in low:
        return "cli_incompatible"
    return None


def classify(outcome: ProcOutcome, parsed: dict | None, *, expect_structured: bool = False) -> str | None:
    if outcome.spawn_error:
        return outcome.spawn_error
    if outcome.cancelled:
        return "cancelled"
    if outcome.timed_out:
        return "timeout"
    if parsed is None or outcome.output_truncated:
        if outcome.exit_code == 0:
            return "invalid_output"
        return classify_text(outcome.stderr) or "transient"
    subtype = parsed.get("subtype")
    if parsed.get("is_error") or subtype != "success":
        if isinstance(subtype, str) and ("max_turns" in subtype or "max_budget" in subtype):
            return "invalid_output"
        result_text = parsed.get("result") if isinstance(parsed.get("result"), str) else ""
        status = _status_int(parsed.get("api_error_status"))
        return classify_text(f"{result_text}\n{outcome.stderr}", status) or "transient"
    if expect_structured and not isinstance(parsed.get("structured_output"), dict):
        return "invalid_output"
    return None


def resolved_model(parsed: dict | None) -> str | None:
    usage = parsed.get("modelUsage") if isinstance(parsed, dict) else None
    if not isinstance(usage, dict) or not usage:
        return None
    best_key, best_out = None, -1
    for key, entry in usage.items():
        out = entry.get("outputTokens", 0) if isinstance(entry, dict) else 0
        out = out if isinstance(out, (int, float)) and not isinstance(out, bool) else 0
        if out > best_out:
            best_key, best_out = key, out
    entry = usage[best_key]
    if isinstance(entry, dict) and isinstance(entry.get("canonicalModel"), str):
        return entry["canonicalModel"]
    return best_key


def _detail(code: str | None, outcome: ProcOutcome, parsed: dict | None) -> str | None:
    if code is None:
        return None
    parts = [code]
    if outcome.exit_code is not None:
        parts.append(f"exit {outcome.exit_code}")
    if parsed:
        if parsed.get("subtype") not in (None, "success"):
            parts.append(f"subtype {parsed.get('subtype')}")
        if parsed.get("api_error_status") is not None:
            parts.append(f"api status {parsed.get('api_error_status')}")
        if parsed.get("is_error") and isinstance(parsed.get("result"), str):
            parts.append(parsed["result"].strip()[:DETAIL_CHARS])
    if outcome.stderr.strip():
        parts.append(outcome.stderr.strip()[:DETAIL_CHARS])
    return "; ".join(parts)


# Auth


def _auth_ok(info: dict, cfg: dict) -> bool:
    if info.get("loggedIn") is not True:
        return False
    method = str(info.get("authMethod") or "").lower()
    if (method == "claude.ai" or "oauth" in method) and info.get("apiProvider") == "firstParty":
        return True
    if cfg.get("allow_api_key", False) and ("key" in method or "api" in method):
        return True
    return False


def check_auth(executable: str, env: dict, cfg: dict, runner=None) -> tuple[bool, str | None]:
    runner = runner or run_process
    outcome = runner(
        [executable, "auth", "status", "--json"],
        cwd=os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else "/",
        env=env,
        stdin_text="",
        timeout=AUTH_TIMEOUT_SECONDS,
    )
    if outcome.spawn_error:
        return False, outcome.spawn_error if outcome.spawn_error != "environment" else "cli_missing"
    if outcome.timed_out:
        return False, "timeout"
    info = parse_output(outcome.stdout)
    if info is None:
        return False, "cli_incompatible"
    if _auth_ok(info, cfg):
        return True, None
    return False, "auth_required"


# Adapter


def _usage_uncertain(outcome: ProcOutcome, parsed: dict | None) -> bool:
    return bool(outcome.timed_out or outcome.cancelled or parsed is None or outcome.output_truncated)


class Adapter:
    def __init__(
        self,
        *,
        executable: str,
        cfg: dict,
        data_dir,
        base_env: dict | None = None,
        runner=None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.executable = executable
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.base_env = dict(os.environ) if base_env is None else dict(base_env)
        self.runner = runner or run_process
        self.clock = clock
        self._auth_ok_until: datetime | None = None

    def _auth(self, env: dict) -> tuple[bool, str | None]:
        now = self.clock()
        if self._auth_ok_until is not None and now < self._auth_ok_until:
            return True, None
        ok, code = check_auth(self.executable, env, self.cfg, runner=self.runner)
        # Only success is cached, so a user who logs in is picked up on the next call.
        self._auth_ok_until = now + AUTH_CACHE if ok else None
        return ok, code

    def _failed(self, code: str, detail: str | None, call_id: str = "", duration_ms: int = 0) -> CallResult:
        return CallResult(
            ok=False, error_code=code, detail=detail, structured=None, text=None, model=None, usage=None,
            cost_usd=None, duration_ms=duration_ms, cli_session_id=None, permission_denials=[],
            exit_code=None, call_id=call_id,
        )

    def call(self, conn_factory, spec: CallSpec, *, job_id: int | None, attempt: int, cancel_event=None) -> CallResult:
        # Built first so an invalid spec raises before any budget slot is taken.
        argv = build_argv(self.executable, spec)
        env = child_env(self.base_env, spec, self.cfg)
        ok, code = self._auth(env)
        if not ok:
            return self._failed(code or "auth_required", f"claude auth check failed: {code}")
        if cancel_event is not None and cancel_event.is_set():
            return self._failed("cancelled", "cancelled before the call started")

        limits = {
            "profile": spec.profile,
            "timeout_seconds": spec.timeout_seconds,
            "tools": list(spec.tools),
            "max_budget_usd": spec.max_budget_usd,
        }
        if spec.profile == "replay":
            limits["allowed_tools_patterns"] = list(spec.allowed_tools_patterns)
            limits["settings"] = spec.settings
        conn = conn_factory()
        try:
            call_id = budget.reserve(
                conn, spec.role, self.cfg, self.clock(),
                job_id=job_id, attempt=attempt, requested_model=spec.model, limits=limits,
            )
        except budget.BudgetExhausted as exc:
            return self._failed("budget_exhausted", str(exc))
        finally:
            conn.close()

        outcome = self.runner(
            argv,
            cwd=spec.cwd,
            env=env,
            stdin_text=spec.prompt,
            timeout=spec.timeout_seconds,
            cancel_event=cancel_event,
        )
        tool_uses: list[dict] = []
        if outcome.spawn_error:
            parsed = None
        elif output_format(spec) == "stream-json":
            parsed, tool_uses = parse_stream(outcome.stdout)
        else:
            parsed = parse_output(outcome.stdout)
        code = classify(outcome, parsed, expect_structured=spec.json_schema is not None)
        result = self._result(call_id, code, outcome, parsed)
        result.tool_uses = tool_uses
        self._settle(conn_factory, result, outcome, parsed)
        return result

    def _result(self, call_id: str, code: str | None, outcome: ProcOutcome, parsed: dict | None) -> CallResult:
        p = parsed or {}
        structured = p.get("structured_output") if isinstance(p.get("structured_output"), dict) else None
        cost = p.get("total_cost_usd")
        denials = p.get("permission_denials")
        return CallResult(
            ok=code is None,
            error_code=code,
            detail=_detail(code, outcome, parsed),
            structured=structured,
            text=p.get("result") if isinstance(p.get("result"), str) else None,
            model=resolved_model(parsed),
            usage=p.get("usage") if isinstance(p.get("usage"), dict) else None,
            cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            duration_ms=outcome.duration_ms,
            cli_session_id=p.get("session_id") if isinstance(p.get("session_id"), str) else None,
            permission_denials=denials if isinstance(denials, list) else [],
            exit_code=outcome.exit_code,
            call_id=call_id,
        )

    def _settle(self, conn_factory, result: CallResult, outcome: ProcOutcome, parsed: dict | None) -> None:
        if result.ok:
            status = "ok"
        elif result.error_code == "cancelled":
            status = "cancelled"
        else:
            status = "error"
        try:
            conn = conn_factory()
        except sqlite3.Error:
            return
        try:
            budget.settle(
                conn, result.call_id, self.clock(), status, result.model, result.usage, result.cost_usd,
                result.duration_ms, result.error_code, result.cli_session_id, _usage_uncertain(outcome, parsed),
            )
        except sqlite3.Error:
            # The row stays 'reserved' and usage_today reports it as unsettled.
            pass
        finally:
            conn.close()


def executable_for(cfg: dict, environ: dict | None = None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("AUTOTUNE_CLAUDE") or cfg.get("claude_executable") or "claude"


def adapter_for(ctx) -> Adapter:
    """The adapter a handler should use: ctx.claude when set (tests), else one
    built from the context's config and data dir and kept on the context."""
    injected = getattr(ctx, "claude", None)
    if injected is not None:
        return injected
    cached = getattr(ctx, "_claude_adapter", None)
    if cached is not None:
        return cached
    clock = ctx.now if callable(getattr(ctx, "now", None)) else utc_now
    adapter = Adapter(executable=executable_for(ctx.config), cfg=ctx.config, data_dir=ctx.data_dir, clock=clock)
    try:
        ctx._claude_adapter = adapter
    except AttributeError:
        pass
    return adapter


def neutral_dir(data_dir) -> Path:
    """An empty 0700 working directory for tool-less calls."""
    root = Path(data_dir).resolve()
    path = root / "neutral"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise RuntimeError(f"neutral dir {path} is inside a git repository at {parent}")
        if parent == root:
            break
    return path
