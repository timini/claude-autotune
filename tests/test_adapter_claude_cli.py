from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import timedelta
from pathlib import Path

from autotune import claude_cli, queue
from autotune.claude_cli import CallSpec, ProcOutcome
from tests._adapter_support import FAKE_CLAUDE, T0, AdapterTestCase, FakeClock, make_cfg, python_argv

FORBIDDEN_FLAGS = ("--bare", "--dangerously-skip-permissions")


def spec(**overrides) -> CallSpec:
    values = dict(
        role="review",
        prompt="Review the episode.\n\nDATA\n{\"episode_id\":\"ep_1\"}",
        system_prompt="You are a reviewer.",
        json_schema={"type": "object"},
        cwd="/",
        model=None,
        timeout_seconds=30,
        tools=[],
        profile="reviewer",
    )
    values.update(overrides)
    return CallSpec(**values)


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class BuildArgvTests(AdapterTestCase):
    def test_reviewer_profile_is_tool_less_and_safe(self):
        argv = claude_cli.build_argv("claude", spec(model="claude-haiku-5-5", max_budget_usd=0.5))
        self.assertEqual(argv[:5], ["claude", "-p", "--output-format", "json", "--no-session-persistence"])
        for flag in ("--safe-mode", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(_flag_value(argv, "--tools"), "")
        self.assertEqual(_flag_value(argv, "--model"), "claude-haiku-5-5")
        self.assertEqual(_flag_value(argv, "--system-prompt"), "You are a reviewer.")
        self.assertEqual(json.loads(_flag_value(argv, "--json-schema")), {"type": "object"})
        self.assertEqual(_flag_value(argv, "--max-budget-usd"), "0.5")
        self.assertNotIn("Review the episode.", " ".join(argv))

    def test_reviewer_ignores_requested_tools(self):
        argv = claude_cli.build_argv("claude", spec(tools=["Bash", "Read"]))
        self.assertEqual(_flag_value(argv, "--tools"), "")

    def test_author_profile_removes_dangerous_tools(self):
        argv = claude_cli.build_argv("claude", spec(role="author", profile="author", tools=["Read", "Bash", "Edit", "WebFetch", "Agent", "Task", "WebSearch"]))
        for flag in ("--safe-mode", "--restricted", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(_flag_value(argv, "--permission-mode"), "acceptEdits")
        self.assertEqual(_flag_value(argv, "--tools"), "Read,Edit")

    def test_author_profile_default_tools(self):
        argv = claude_cli.build_argv("claude", spec(role="author", profile="author", tools=[]))
        self.assertEqual(_flag_value(argv, "--tools"), "Read,Edit,Write,Glob,Grep")

    def test_replay_profile(self):
        argv = claude_cli.build_argv("claude", spec(role="replay", profile="replay", tools=["Read", "Bash"], add_dirs=["/a", "/b"], json_schema=None, system_prompt=None))
        self.assertNotIn("--safe-mode", argv)
        self.assertEqual(_flag_value(argv, "--setting-sources"), "project")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(_flag_value(argv, "--permission-mode"), "dontAsk")
        self.assertEqual(_flag_value(argv, "--tools"), "Read,Bash")
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"], ["/a", "/b"])
        self.assertNotIn("--json-schema", argv)
        self.assertNotIn("--system-prompt", argv)
        empty = claude_cli.build_argv("claude", spec(role="replay", profile="replay", tools=[]))
        self.assertEqual(_flag_value(empty, "--tools"), "")

    def test_no_profile_uses_forbidden_flags(self):
        for profile, role in (("reviewer", "review"), ("author", "author"), ("replay", "replay")):
            argv = claude_cli.build_argv("claude", spec(role=role, profile=profile))
            for flag in FORBIDDEN_FLAGS:
                self.assertNotIn(flag, argv)

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            claude_cli.build_argv("claude", spec(profile="admin"))


class ChildEnvTests(AdapterTestCase):
    BASE = {
        "PATH": "/bin",
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_BASE_URL": "http://x",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SSE_PORT": "1234",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "CLAUDE_CONFIG_DIR": "/cfg",
    }

    def test_strips_provider_and_parent_session_variables(self):
        env = claude_cli.child_env(self.BASE, spec(extra_env={"EXTRA": "1"}), make_cfg())
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDE_CODE_SSE_PORT"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/cfg")
        self.assertEqual(env["AUTOTUNE_ORIGIN"], "worker")
        self.assertEqual(env["AUTOTUNE_NO_WORKER"], "1")
        self.assertEqual(env["EXTRA"], "1")
        self.assertEqual(self.BASE["ANTHROPIC_API_KEY"], "k")

    def test_allow_api_key_keeps_provider_variables(self):
        env = claude_cli.child_env(self.BASE, spec(), make_cfg(allow_api_key=True))
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
        self.assertNotIn("CLAUDECODE", env)

    def test_origin_per_role(self):
        expected = {"review": "worker", "judge": "judge", "curator": "worker", "author": "author", "replay": "replay"}
        for role, origin in expected.items():
            self.assertEqual(claude_cli.child_env({}, spec(role=role), make_cfg())["AUTOTUNE_ORIGIN"], origin)


class ClassifyTests(AdapterTestCase):
    def _err(self, **parsed) -> str | None:
        base = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {}}
        base.update(parsed)
        return claude_cli.classify(ProcOutcome(exit_code=1, stdout="", stderr=""), base, expect_structured=True)

    def test_api_errors(self):
        cases = [
            ({"is_error": True, "api_error_status": 401}, "auth_required"),
            ({"is_error": True, "result": "Not logged in. Please run /login"}, "auth_required"),
            ({"is_error": True, "api_error_status": 429}, "rate_limited"),
            ({"is_error": True, "api_error_status": 429, "result": "Claude AI usage limit reached|1700000000"}, "usage_limit"),
            ({"is_error": True, "result": "quota exceeded"}, "usage_limit"),
            # A transient rate limit is not the daily usage limit, whatever its wording.
            ({"is_error": True, "api_error_status": 429, "result": "Rate limit reached for requests"}, "rate_limited"),
            ({"is_error": True, "result": "Rate limit reached for requests"}, "rate_limited"),
            ({"is_error": True, "result": "Monthly limit reached"}, "usage_limit"),
            ({"is_error": True, "api_error_status": 529}, "overloaded"),
            ({"is_error": True, "result": "API Error: Overloaded"}, "overloaded"),
            ({"is_error": True, "api_error_status": 503}, "server_error"),
            ({"is_error": True, "subtype": "error_max_turns"}, "invalid_output"),
            ({"is_error": True, "subtype": "error_max_budget_usd"}, "invalid_output"),
            ({"is_error": True, "result": "something odd"}, "transient"),
            ({"structured_output": None}, "invalid_output"),
            ({"stop_reason": "tool_use"}, None),
        ]
        for parsed, code in cases:
            with self.subTest(parsed=parsed):
                self.assertEqual(self._err(**parsed), code)
                if code is not None:
                    self.assertIn(code, claude_cli.ERROR_CODES)

    def test_process_level_outcomes(self):
        self.assertEqual(claude_cli.classify(ProcOutcome(None, "", "", spawn_error="cli_missing"), None), "cli_missing")
        self.assertEqual(claude_cli.classify(ProcOutcome(None, "", "", cancelled=True), None), "cancelled")
        self.assertEqual(claude_cli.classify(ProcOutcome(None, "", "", timed_out=True), None), "timeout")
        self.assertEqual(claude_cli.classify(ProcOutcome(0, "not json", ""), None), "invalid_output")
        self.assertEqual(claude_cli.classify(ProcOutcome(1, "", "error: unknown option '--safe-mode'"), None), "cli_incompatible")
        self.assertEqual(claude_cli.classify(ProcOutcome(1, "", "segfault"), None), "transient")

    def test_every_error_code_has_a_queue_class(self):
        # Codes A must add to queue._CODE_CLASSES; this lists any still missing.
        missing = [c for c in claude_cli.ERROR_CODES if c not in queue._CODE_CLASSES and c != "cancelled"]
        self.assertEqual(missing, [])

    def test_resolved_model_prefers_most_output(self):
        parsed = {"modelUsage": {
            "claude-haiku-5-5": {"outputTokens": 3, "canonicalModel": "claude-haiku-5-5"},
            "opus-alias": {"outputTokens": 50, "canonicalModel": "claude-opus-5-5"},
        }}
        self.assertEqual(claude_cli.resolved_model(parsed), "claude-opus-5-5")
        self.assertEqual(claude_cli.resolved_model({"modelUsage": {"m1": {"outputTokens": 1}}}), "m1")
        self.assertIsNone(claude_cli.resolved_model({}))


class RunProcessTests(AdapterTestCase):
    def test_missing_executable(self):
        out = claude_cli.run_process([str(self.tmp / "nope")], cwd=str(self.tmp), env={}, stdin_text="", timeout=5)
        self.assertEqual(out.spawn_error, "cli_missing")

    def test_output_cap_marks_truncation(self):
        out = claude_cli.run_process(
            python_argv("import sys; sys.stdout.write('x' * 100000)"),
            cwd=str(self.tmp), env=dict(os.environ), stdin_text="", timeout=10, max_output_bytes=1000,
        )
        self.assertEqual(len(out.stdout), 1000)
        self.assertTrue(out.output_truncated)
        self.assertEqual(claude_cli.classify(out, None), "invalid_output")

    def test_exception_in_the_poll_loop_still_kills_the_process_group(self):
        pid_file = self.tmp / "child.pid"
        code = (f"import os, subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pid_file) + '.tmp'!r}, 'w').write(f'{{os.getpid()}} {{p.pid}}'); "
                f"os.replace({str(pid_file) + '.tmp'!r}, {str(pid_file)!r}); time.sleep(60)")

        class Interrupting:
            # Stands in for a signal arriving while the worker waits on the call.
            def is_set(self):
                if pid_file.exists():
                    raise KeyboardInterrupt
                return False

        with self.assertRaises(KeyboardInterrupt):
            claude_cli.run_process(python_argv(code), cwd=str(self.tmp), env=dict(os.environ), stdin_text="",
                                   timeout=30, cancel_event=Interrupting())
        child, grandchild = (int(p) for p in pid_file.read_text().split())
        self.assertTrue(_wait_gone(child), "child survived")
        self.assertTrue(_wait_gone(grandchild), "grandchild survived")

    def test_large_stdin_does_not_deadlock(self):
        text = "y" * (2 * 1024 * 1024)
        out = claude_cli.run_process(
            python_argv("import sys; data = sys.stdin.read(); print(len(data))"),
            cwd=str(self.tmp), env=dict(os.environ), stdin_text=text, timeout=20,
        )
        self.assertEqual(out.stdout.strip(), str(len(text)))


class AdapterTests(AdapterTestCase):
    def adapter(self, cfg=None, clock=None, runner=None) -> claude_cli.Adapter:
        env = dict(self.base_env)
        env["ANTHROPIC_API_KEY"] = "sk-test"
        env["CLAUDECODE"] = "1"
        return claude_cli.Adapter(
            executable=str(FAKE_CLAUDE), cfg=cfg or make_cfg(), data_dir=self.data_dir,
            base_env=env, runner=runner, clock=clock or FakeClock(),
        )

    def call(self, adapter, s=None, cancel_event=None):
        return adapter.call(self.conn_factory, s or spec(cwd=str(claude_cli.neutral_dir(self.data_dir))),
                            job_id=None, attempt=1, cancel_event=cancel_event)

    def test_success_records_usage_and_model(self):
        self.write_script([{
            "structured_output": {"episode_id": "$echo_episode", "n": 1},
            "result": "ok", "model": "claude-haiku-5-5",
            "usage": {"input_tokens": 120, "output_tokens": 30}, "cost_usd": 0.0123,
            "session_id": "cli-sess-9",
        }])
        result = self.call(self.adapter())
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.structured, {"episode_id": "ep_1", "n": 1})
        self.assertEqual(result.model, "claude-haiku-5-5")
        rows = self.model_calls()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["call_id"], result.call_id)
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["resolved_model"], "claude-haiku-5-5")
        self.assertEqual(json.loads(row["usage_json"]), {"input_tokens": 120, "output_tokens": 30})
        self.assertAlmostEqual(row["cost_usd"], 0.0123)
        self.assertEqual(row["cli_session_id"], "cli-sess-9")
        self.assertEqual(row["usage_uncertain"], 0)
        self.assertIsNotNone(row["duration_ms"])

    def test_child_sees_stdin_prompt_neutral_cwd_and_clean_env(self):
        s = spec(cwd=str(claude_cli.neutral_dir(self.data_dir)))
        self.call(self.adapter(), s)
        (logged,) = self.fake_calls()
        self.assertEqual(logged["stdin"], s.prompt)
        self.assertEqual(os.path.realpath(logged["cwd"]), os.path.realpath(s.cwd))
        self.assertFalse(logged["env"]["ANTHROPIC_API_KEY"])
        self.assertFalse(logged["env"]["CLAUDECODE"])
        self.assertEqual(logged["env"]["AUTOTUNE_ORIGIN"], "worker")
        self.assertEqual(logged["env"]["AUTOTUNE_NO_WORKER"], "1")
        self.assertIn("--safe-mode", logged["argv"])
        self.assertNotIn("--bare", logged["argv"])

    def test_spawned_grandchild_inherits_recursion_guard(self):
        out = self.tmp / "guard.json"
        code = ("import json, os, sys; json.dump({k: os.environ.get(k) for k in "
                "('AUTOTUNE_NO_WORKER', 'AUTOTUNE_ORIGIN', 'ANTHROPIC_API_KEY')}, open(sys.argv[1], 'w'))")
        self.write_script([{"structured_output": {}, "spawn": python_argv(code) + [str(out)]}])
        self.call(self.adapter(), spec(role="judge", cwd=str(claude_cli.neutral_dir(self.data_dir))))
        self.assertEqual(json.loads(out.read_text()), {"AUTOTUNE_NO_WORKER": "1", "AUTOTUNE_ORIGIN": "judge", "ANTHROPIC_API_KEY": None})

    def test_timeout_kills_the_process_group(self):
        pid_file = self.tmp / "grandchild.pid"
        # The fake writes the sleeper's pid itself, right after starting it.
        self.write_script([{"spawn_background": python_argv("import time; time.sleep(60)"),
                            "spawn_background_pid_file": str(pid_file), "sleep": 60, "structured_output": {}}])
        started = time.monotonic()
        result = self.call(self.adapter(), spec(cwd=str(self.tmp), timeout_seconds=3))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.error_code, "timeout")
        self.assertFalse(result.ok)
        fake_pid = self.fake_calls()[0]["pid"]
        if not pid_file.exists():
            self.fail("the fake did not start its background sleeper before the timeout")
        grandchild = int(pid_file.read_text())
        self.assertTrue(_wait_gone(fake_pid), "fake claude survived")
        self.assertTrue(_wait_gone(grandchild), "grandchild sleeper survived")
        (row,) = self.model_calls()
        self.assertEqual(row["outcome"], "error")
        self.assertEqual(row["error_code"], "timeout")
        self.assertEqual(row["usage_uncertain"], 1)

    def test_cancel_event_stops_the_call(self):
        self.write_script([{"sleep": 60, "structured_output": {}}])
        event = threading.Event()
        threading.Timer(0.7, event.set).start()
        started = time.monotonic()
        result = self.call(self.adapter(), cancel_event=event)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result.error_code, "cancelled")
        self.assertTrue(_wait_gone(self.fake_calls()[0]["pid"]))
        (row,) = self.model_calls()
        self.assertEqual(row["outcome"], "cancelled")
        self.assertEqual(row["usage_uncertain"], 1)

    def test_cancel_before_start_makes_no_call(self):
        event = threading.Event()
        event.set()
        result = self.call(self.adapter(), cancel_event=event)
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual(self.fake_calls(), [])
        self.assertEqual(self.model_calls(), [])

    def test_budget_exhaustion_blocks_without_running(self):
        adapter = self.adapter(cfg=make_cfg(review_calls_per_day=1))
        self.assertTrue(self.call(adapter).ok)
        second = self.call(adapter)
        self.assertEqual(second.error_code, "budget_exhausted")
        self.assertEqual(queue.classify_error(second.error_code), "budget_exhausted")
        self.assertEqual(len(self.fake_calls()), 1)
        self.assertEqual(len(self.model_calls()), 1)

    def test_auth_failure_blocks_before_any_model_call_row(self):
        cases = [
            ({"loggedIn": False, "authMethod": "none"}, "auth_required"),
            ({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}, "auth_required"),
            ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}, "auth_required"),
        ]
        for info, code in cases:
            with self.subTest(info=info):
                self.base_env["AUTOTUNE_FAKE_AUTH"] = json.dumps(info)
                result = self.call(self.adapter())
                self.assertEqual(result.error_code, code)
                self.assertEqual(self.model_calls(), [])
                self.assertEqual(self.fake_calls(), [])
        self.base_env["AUTOTUNE_FAKE_AUTH"] = "garbage"
        self.assertEqual(self.call(self.adapter()).error_code, "cli_incompatible")
        self.assertEqual(self.model_calls(), [])

    def test_api_key_auth_allowed_when_configured(self):
        self.base_env["AUTOTUNE_FAKE_AUTH"] = json.dumps({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})
        self.assertTrue(self.call(self.adapter(cfg=make_cfg(allow_api_key=True))).ok)

    def test_successful_auth_is_cached_for_ten_minutes(self):
        auth_runs = []

        def runner(argv, **kwargs):
            if argv[1:3] == ["auth", "status"]:
                auth_runs.append(1)
            return claude_cli.run_process(argv, **kwargs)

        clock = FakeClock()
        adapter = self.adapter(clock=clock, runner=runner)
        self.call(adapter)
        clock.now = T0 + timedelta(minutes=9)
        self.call(adapter)
        self.assertEqual(len(auth_runs), 1)
        clock.now = T0 + timedelta(minutes=11)
        self.call(adapter)
        self.assertEqual(len(auth_runs), 2)

    def test_malformed_stdout_is_invalid_output(self):
        self.write_script([{"stdout_raw": "Here is your review: {not json"}])
        result = self.call(self.adapter())
        self.assertEqual(result.error_code, "invalid_output")
        (row,) = self.model_calls()
        self.assertEqual(row["outcome"], "error")
        self.assertEqual(row["usage_uncertain"], 1)

    def test_missing_structured_output_is_invalid_output(self):
        self.write_script([{"result": "plain text only"}])
        result = self.call(self.adapter())
        self.assertEqual(result.error_code, "invalid_output")
        self.assertEqual(result.text, "plain text only")

    def test_rate_limit_error_classified(self):
        self.write_script([{"is_error": True, "api_error_status": 429, "result": "API Error: 429", "exit_code": 1}])
        result = self.call(self.adapter())
        self.assertEqual(result.error_code, "rate_limited")
        self.assertIn("api status 429", result.detail)

    def test_missing_executable_is_cli_missing(self):
        adapter = claude_cli.Adapter(executable=str(self.tmp / "no-claude"), cfg=make_cfg(), data_dir=self.data_dir,
                                     base_env=self.base_env, clock=FakeClock())
        result = self.call(adapter)
        self.assertEqual(result.error_code, "cli_missing")
        self.assertEqual(self.model_calls(), [])

    def test_responses_are_picked_by_role(self):
        self.write_script([
            {"role": "author", "structured_output": {"who": "author"}},
            {"role": "worker", "structured_output": {"who": "reviewer"}},
        ])
        adapter = self.adapter()
        self.assertEqual(self.call(adapter).structured, {"who": "reviewer"})
        self.assertEqual(self.call(adapter).structured, {"who": "reviewer"})
        author = spec(role="author", profile="author", cwd=str(self.tmp))
        self.assertEqual(self.call(adapter, author).structured, {"who": "author"})


class FakeEditsTests(AdapterTestCase):
    def test_edits_apply_under_cwd_and_refuse_escape(self):
        work = self.tmp / "wt"
        work.mkdir()
        adapter = claude_cli.Adapter(executable=str(FAKE_CLAUDE), cfg=make_cfg(), data_dir=self.data_dir,
                                     base_env=self.base_env, clock=FakeClock())
        self.write_script([{"edits": {"CLAUDE.md": "hello\n", "a/b.txt": "x"}, "chmod": {"a/b.txt": "755"}, "structured_output": {}}])
        s = spec(role="author", profile="author", cwd=str(work))
        self.assertTrue(adapter.call(self.conn_factory, s, job_id=None, attempt=1).ok)
        self.assertEqual((work / "CLAUDE.md").read_text(), "hello\n")
        self.assertTrue(os.stat(work / "a/b.txt").st_mode & stat.S_IXUSR)
        self.write_script([{"edits": {"../escape.txt": "x"}, "structured_output": {}}])
        result = adapter.call(self.conn_factory, s, job_id=None, attempt=1)
        self.assertFalse(result.ok)
        self.assertFalse((self.tmp / "escape.txt").exists())


class AdapterForTests(AdapterTestCase):
    class Ctx:
        def __init__(self, data_dir, config):
            self.data_dir = data_dir
            self.config = config
            self.claude = None

        def now(self):
            return T0

    def test_injected_adapter_wins(self):
        ctx = self.Ctx(self.data_dir, make_cfg())
        sentinel = object()
        ctx.claude = sentinel
        self.assertIs(claude_cli.adapter_for(ctx), sentinel)

    def test_executable_from_env_then_config(self):
        cfg = make_cfg(claude_executable="/opt/claude")
        self.assertEqual(claude_cli.executable_for(cfg, {"AUTOTUNE_CLAUDE": "/x/fake"}), "/x/fake")
        self.assertEqual(claude_cli.executable_for(cfg, {}), "/opt/claude")
        self.assertEqual(claude_cli.executable_for(make_cfg(), {}), "claude")

    def test_built_adapter_reused_per_context(self):
        ctx = self.Ctx(self.data_dir, make_cfg(claude_executable="/opt/claude"))
        first = claude_cli.adapter_for(ctx)
        self.assertIs(first, claude_cli.adapter_for(ctx))
        self.assertEqual(Path(first.data_dir), self.data_dir)


class NeutralDirTests(AdapterTestCase):
    def test_neutral_dir_is_private_and_empty(self):
        path = claude_cli.neutral_dir(self.data_dir)
        (path / "left.txt").write_text("x")
        (path / "sub").mkdir()
        path = claude_cli.neutral_dir(self.data_dir)
        self.assertEqual(list(path.iterdir()), [])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        self.assertEqual(path.parent, self.data_dir.resolve())

    def test_neutral_dir_refuses_a_repository(self):
        (self.data_dir / ".git").mkdir()
        with self.assertRaises(RuntimeError):
            claude_cli.neutral_dir(self.data_dir)
