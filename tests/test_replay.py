from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

from autotune import claude_cli, replay
from autotune.claude_cli import CallSpec, ProcOutcome
from tests._adapter_support import FAKE_CLAUDE, AdapterTestCase, FakeClock, make_cfg
from tests._git_support import GitRepoTestCase, git, snapshot, write

MARKER = "Always run the tests and report the observed counts."


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def _allowed(argv: list[str]) -> list[str]:
    if "--allowedTools" not in argv:
        return []
    values = []
    for arg in argv[argv.index("--allowedTools") + 1:]:
        if arg.startswith("--"):
            break
        values.append(arg)
    return values


def _replay_spec(**overrides) -> CallSpec:
    values = dict(
        role="replay", prompt="do it", system_prompt=None, json_schema=None, cwd="/", model=None,
        timeout_seconds=30, tools=["Read", "Edit", "Bash"], profile="replay",
    )
    values.update(overrides)
    return CallSpec(**values)


def _case(**overrides) -> dict:
    case = {
        "id": "report-observed-test-status",
        "task_family": "verification-reporting",
        "kind": "target",
        "critical": True,
        "description": "d",
        "request": "Run the tests and tell me if they pass.",
        "fixture": {"files": {"tests/test_x.py": "import unittest\n", "notes.txt": "hello\n"}, "from_commit": None},
        "harness_paths": ["CLAUDE.md", ".claude/rules/**"],
        "allowed_tools": ["Read", "Grep", "Edit", "Bash"],
        "allowed_bash": ["python3 -m unittest*"],
        "network": "not_required",
        "trials_per_arm": 1,
        "timeout_seconds": 30,
        "graders": [],
        "judge": None,
    }
    case.update(overrides)
    return case


class StreamArgvTests(AdapterTestCase):
    def test_replay_uses_stream_json_allow_rules_and_settings(self):
        argv = claude_cli.build_argv("claude", _replay_spec(
            allowed_tools_patterns=["Edit", "Bash(python3 -m unittest*)"],
            settings={"autoMemoryEnabled": False},
        ))
        self.assertEqual(argv[1:6], ["-p", "--output-format", "stream-json", "--no-session-persistence", "--verbose"])
        self.assertEqual(_allowed(argv), ["Edit", "Bash(python3 -m unittest*)"])
        self.assertEqual(argv.count("--allowedTools"), 1)
        self.assertEqual(json.loads(_flag_values(argv, "--settings")[0]), {"autoMemoryEnabled": False})
        self.assertEqual(_flag_values(argv, "--setting-sources"), ["project"])
        self.assertNotIn("--safe-mode", argv)

    def test_reviewer_and_author_argv_unchanged(self):
        for profile, role in (("reviewer", "review"), ("author", "author")):
            argv = claude_cli.build_argv("claude", _replay_spec(profile=profile, role=role))
            self.assertEqual(argv[1:5], ["-p", "--output-format", "json", "--no-session-persistence"])
            self.assertNotIn("--verbose", argv)
            self.assertNotIn("--allowedTools", argv)
            self.assertNotIn("--settings", argv)

    def test_replay_only_options_rejected_elsewhere(self):
        with self.assertRaises(ValueError):
            claude_cli.build_argv("claude", _replay_spec(profile="reviewer", role="review", settings={"a": 1}))
        with self.assertRaises(ValueError):
            claude_cli.build_argv("claude", _replay_spec(profile="author", role="author", allowed_tools_patterns=["Edit"]))

    def test_pattern_with_newline_rejected(self):
        for bad in ("Bash(ls\n)", "", "Bash(a\x00)"):
            with self.assertRaises(ValueError):
                claude_cli.build_argv("claude", _replay_spec(allowed_tools_patterns=[bad]))


class ParseStreamTests(AdapterTestCase):
    def test_tool_uses_and_result(self):
        lines = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "x"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python3 -m unittest " + "v" * 400}},
            ]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}},
            "not json",
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a"}}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "done"},
        ]
        text = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
        result, uses = claude_cli.parse_stream(text)
        self.assertEqual(result["result"], "done")
        self.assertEqual([u["name"] for u in uses], ["Bash", "Read"])
        self.assertTrue(uses[0]["is_error"])
        self.assertIsNone(uses[1]["is_error"])
        self.assertLessEqual(len(uses[0]["input_summary"]), 300)
        self.assertTrue(uses[0]["input_summary"].startswith('{"command":"python3 -m unittest'))

    def test_stream_without_result_line_is_invalid_output(self):
        text = json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
        parsed, _ = claude_cli.parse_stream(text)
        self.assertIsNone(parsed)
        self.assertEqual(claude_cli.classify(ProcOutcome(0, text, ""), parsed), "invalid_output")


class FakeStreamTests(AdapterTestCase):
    def adapter(self) -> claude_cli.Adapter:
        return claude_cli.Adapter(
            executable=str(FAKE_CLAUDE), cfg=make_cfg(), data_dir=self.data_dir,
            base_env=self.base_env, clock=FakeClock(),
        )

    def test_adapter_reads_tool_uses_from_fake_stream(self):
        self.write_script([{
            "result": "All 3 tests passed.",
            "model": "claude-fake-2",
            "tool_uses": [{"name": "Bash", "input": {"command": "python3 -m unittest"}, "result": "Ran 3 tests"}],
        }])
        spec = _replay_spec(cwd=str(self.tmp), allowed_tools_patterns=["Bash(python3 -m unittest*)"])
        result = self.adapter().call(self.conn_factory, spec, job_id=None, attempt=1)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.text, "All 3 tests passed.")
        self.assertEqual(result.model, "claude-fake-2")
        self.assertEqual(result.tool_uses, [{"name": "Bash", "input_summary": '{"command":"python3 -m unittest"}', "is_error": False}])
        rows = self.model_calls()
        self.assertEqual([r["outcome"] for r in rows], ["ok"])
        self.assertEqual(rows[0]["role"], "replay")

    def test_reviewer_call_still_json_with_no_tool_uses(self):
        self.write_script([{"structured_output": {"ok": True}, "tool_uses": [{"name": "Bash"}]}])
        spec = _replay_spec(profile="reviewer", role="review", json_schema={"type": "object"}, cwd=str(self.tmp), tools=[])
        result = self.adapter().call(self.conn_factory, spec, job_id=None, attempt=1)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.structured, {"ok": True})
        self.assertEqual(result.tool_uses, [])

    def test_fake_requires_verbose_for_stream_json(self):
        outcome = claude_cli.run_process(
            [str(FAKE_CLAUDE), "-p", "--output-format", "stream-json"],
            cwd=str(self.tmp), env=self.base_env, stdin_text="", timeout=20,
        )
        self.assertEqual(outcome.exit_code, 1)
        self.assertIn("requires --verbose", outcome.stderr)

    def test_fake_chooses_response_by_workspace_file(self):
        self.write_script([
            {"when_file_contains": {"path": "CLAUDE.md", "text": "MARK"}, "result": "with marker"},
            {"when_file_not_contains": {"path": "CLAUDE.md", "text": "MARK"}, "role": "replay", "result": "without marker"},
            {"result": "plain"},
        ])
        ws = self.tmp / "ws"
        ws.mkdir()
        spec = _replay_spec(cwd=str(ws))
        adapter = self.adapter()
        self.assertEqual(adapter.call(self.conn_factory, spec, job_id=None, attempt=1).text, "without marker")
        (ws / "CLAUDE.md").write_text("MARK\n", encoding="utf-8")
        self.assertEqual(adapter.call(self.conn_factory, spec, job_id=None, attempt=1).text, "with marker")
        self.assertEqual(adapter.call(self.conn_factory, spec, job_id=None, attempt=1).text, "with marker")
        review = _replay_spec(profile="reviewer", role="review", cwd=str(self.tmp / "data"), tools=[])
        self.assertEqual(adapter.call(self.conn_factory, review, job_id=None, attempt=1).text, "plain")
        logged = [c["claude_md"] for c in self.fake_calls()]
        self.assertEqual(logged[:2], [None, "MARK\n"])

    def test_fake_version(self):
        outcome = claude_cli.run_process([str(FAKE_CLAUDE), "--version"], cwd=str(self.tmp), env=self.base_env, stdin_text="", timeout=20)
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("(Claude Code)", outcome.stdout)


class ReplayRepoTestCase(GitRepoTestCase):
    base_files = {
        "CLAUDE.md": "# Project rules\n",
        ".claude/rules/testing.md": "Run tests.\n",
        "src/app.py": "print('hi')\n",
        "src/run.sh": "#!/bin/sh\necho run\n",
        ".autotune/cases/report-observed-test-status.json": "{}\n",
        ".gitignore": "build/\n",
    }

    def setUp(self):
        super().setUp()
        os.chmod(self.repo / "src/run.sh", 0o755)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "exec bit")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "checkout", "-q", "-b", "autotune/cand_1")
        write(self.repo, "CLAUDE.md", f"# Project rules\n{MARKER}\n")
        write(self.repo, ".claude/rules/new.md", "New rule.\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "candidate")
        self.cand = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "checkout", "-q", "main")
        if self.marker.exists():
            self.marker.unlink()  # the fixture setup's own commits ran the user hooks
        replay._cli_build_cache.clear()
        self.root = self.tmp / "replay-tmp" / replay.ROOT_NAME
        patcher = mock.patch.object(replay, "replay_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def both(self, case, **kw):
        a = replay.materialize(self.repo, case, self.base, self.base, "run_base", **kw)
        b = replay.materialize(self.repo, case, self.cand, self.base, "run_cand", **kw)
        self.addCleanup(lambda: [replay.cleanup(w) for w in (a, b)])
        return a, b


class MaterializeTests(ReplayRepoTestCase):
    def test_same_fixture_distinct_harness(self):
        before = snapshot(self.repo)
        case = _case(fixture={
            "files": {"notes.txt": "hello\n", "CLAUDE.md": "fixture copy is dropped\n"},
            "from_commit": {"paths": ["src/**", ".autotune/**"]},
        })
        a, b = self.both(case)
        self.assertEqual(a.fixture_digests, b.fixture_digests)
        self.assertEqual(a.fixture_digest, b.fixture_digest)
        self.assertEqual(sorted(a.fixture_digests), ["notes.txt", "src/app.py", "src/run.sh"])
        self.assertEqual(a.arm_commit, self.base)
        self.assertEqual(b.arm_commit, self.cand)
        self.assertEqual(a.base_commit, b.base_commit)
        self.assertEqual(a.overlaid_paths, [".claude/rules/testing.md", "CLAUDE.md"])
        self.assertEqual(b.overlaid_paths, [".claude/rules/new.md", ".claude/rules/testing.md", "CLAUDE.md"])
        a_md = Path(a.path, "CLAUDE.md").read_text(encoding="utf-8")
        b_md = Path(b.path, "CLAUDE.md").read_text(encoding="utf-8")
        self.assertNotIn(MARKER, a_md)
        self.assertIn(MARKER, b_md)
        self.assertFalse(Path(a.path, ".autotune").exists())
        self.assertTrue(os.stat(Path(a.path, "src/run.sh")).st_mode & stat.S_IXUSR)
        self.assertFalse(os.stat(Path(a.path, "src/app.py")).st_mode & stat.S_IXUSR)
        self.assertEqual(git(a.path, "status", "--porcelain"), "")
        self.assertEqual(git(a.path, "log", "-1", "--format=%an <%ae>").strip(), "Autotune <autotune@localhost.invalid>")
        self.assertEqual(git(a.path, "rev-parse", "--show-toplevel").strip(), a.path)
        self.assertEqual(Path(a.path).parent, self.root)
        self.assertEqual(snapshot(self.repo), before)
        self.assertFalse(self.marker.exists(), "no hook may run")

    def test_editable_globs_are_overlaid(self):
        _, b = self.both(_case(harness_paths=[]), editable_globs=[".claude/**"])
        self.assertEqual(b.overlaid_paths, [".claude/rules/new.md", ".claude/rules/testing.md"])
        self.assertFalse(Path(b.path, "CLAUDE.md").exists())

    def test_escaping_fixture_paths_rejected_and_workspace_removed(self):
        for bad in ("../x", "/etc/x", "a/../../x", ".git/hooks/pre-commit", "a//b"):
            with self.subTest(bad=bad):
                with self.assertRaises(replay.ReplayError) as cm:
                    replay.materialize(self.repo, _case(fixture={"files": {bad: "x"}}), self.base, self.base, "run_bad")
                self.assertEqual(cm.exception.code, "invalid_case")
                self.assertFalse((self.root / "run_bad").exists())
        self.assertFalse((self.tmp / "replay-tmp" / "x").exists())

    def test_invalid_run_id_rejected(self):
        for bad in ("../up", "", "a/b"):
            with self.assertRaises(replay.ReplayError):
                replay.materialize(self.repo, _case(), self.base, self.base, bad)

    def test_root_inside_repo_or_data_dir_refused(self):
        with mock.patch.object(replay, "replay_root", return_value=self.repo / "tmp" / replay.ROOT_NAME):
            with self.assertRaises(replay.ReplayError) as cm:
                replay.materialize(self.repo, _case(), self.base, self.base, "run_x")
        self.assertEqual(cm.exception.code, "workspace_unsafe")
        with self.assertRaises(replay.ReplayError):
            replay.materialize(self.repo, _case(), self.base, self.base, "run_x", data_dir=self.tmp / "replay-tmp")
        self.assertFalse((self.repo / "tmp").exists())

    def test_root_that_is_a_symlink_or_not_ours_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.root.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, self.root)
        with self.assertRaises(replay.ReplayError) as cm:
            replay.materialize(self.repo, _case(), self.base, self.base, "run_link")
        self.assertEqual(cm.exception.code, "workspace_unsafe")
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(self.root)
        self.root.mkdir()
        with mock.patch.object(replay.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(replay.ReplayError) as cm:
                replay.materialize(self.repo, _case(), self.base, self.base, "run_other")
        self.assertEqual(cm.exception.code, "workspace_unsafe")
        self.assertEqual(os.listdir(self.root), [])

    def test_shared_root_mode_is_tightened(self):
        self.root.mkdir(parents=True)
        os.chmod(self.root, 0o777)
        ws = replay.materialize(self.repo, _case(), self.base, self.base, "run_mode")
        self.addCleanup(replay.cleanup, ws)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)

    def test_ancestor_customizations_recorded(self):
        a, _ = self.both(_case())
        self.assertEqual(a.ancestor_customizations, [])
        write(self.tmp / "replay-tmp", "CLAUDE.md", "stray\n")
        (self.tmp / ".claude").mkdir()
        ws = replay.materialize(self.repo, _case(), self.base, self.base, "run_anc")
        self.addCleanup(replay.cleanup, ws)
        self.assertTrue(ws.ancestor_customization_present)
        self.assertIn(str(self.tmp / "replay-tmp" / "CLAUDE.md"), ws.ancestor_customizations)
        self.assertIn(str(self.tmp / ".claude"), ws.ancestor_customizations)

    def test_cleanup_only_removes_replay_workspaces(self):
        ws = replay.materialize(self.repo, _case(), self.base, self.base, "run_rm")
        locked = Path(ws.path, "locked")
        locked.mkdir()
        write(locked, "f", "x")
        os.chmod(locked, 0o500)
        self.assertTrue(replay.cleanup(ws))
        self.assertFalse(Path(ws.path).exists())
        self.assertFalse(replay.cleanup(ws))
        for other in (self.repo, self.root, self.tmp / "elsewhere" / "run_rm", self.root / "a" / "b"):
            with self.assertRaises(replay.ReplayError):
                replay.cleanup(other)
        self.assertTrue(self.repo.exists())

    def test_default_root_is_under_system_temp(self):
        mock.patch.stopall()
        root = replay.replay_root()
        self.assertEqual(root, Path(os.path.realpath(tempfile.gettempdir())) / "autotune-replay")


class RunArmTests(ReplayRepoTestCase):
    def setUp(self):
        super().setUp()
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        from autotune import store
        self.db_path = self.data_dir / "autotune.db"
        store.connect(self.db_path).close()
        self.log_path = self.tmp / "fake.log"
        self.script_path = self.tmp / "script.json"
        self.script_path.write_text(json.dumps({"responses": [
            {"when_file_contains": {"path": "CLAUDE.md", "text": MARKER}, "result": "Ran 3 tests: OK (observed).",
             "model": "claude-fake-1",
             "tool_uses": [{"name": "Bash", "input": {"command": "python3 -m unittest"}, "result": "Ran 3 tests"}]},
            {"result": "The tests should pass.", "model": "claude-fake-1"},
        ]}), encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.tmp)),
            "AUTOTUNE_FAKE_LOG": str(self.log_path),
            "AUTOTUNE_FAKE_SCRIPT": str(self.script_path),
            "AUTOTUNE_FAKE_VERSION": "9.9.9 (Claude Code)",
            "ANTHROPIC_API_KEY": "must-be-stripped",
        }
        self.runs: list[ProcOutcome] = []

        def runner(argv, **kw):
            outcome = claude_cli.run_process(argv, **kw)
            self.runs.append(argv)
            return outcome

        self.adapter = claude_cli.Adapter(
            executable=str(FAKE_CLAUDE), cfg=make_cfg(), data_dir=self.data_dir, base_env=env,
            runner=runner, clock=FakeClock(),
        )

    def conn_factory(self):
        from autotune import store
        return store.connect(self.db_path)

    def fake_calls(self):
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def test_candidate_harness_changes_the_replay(self):
        a, b = self.both(_case())
        cfg = make_cfg()
        base = replay.run_arm(self.adapter, a, _case(), "baseline", None, cfg, conn_factory=self.conn_factory, job_id=None, attempt=1)
        cand = replay.run_arm(self.adapter, b, _case(), "candidate", "claude-fake-1", cfg, conn_factory=self.conn_factory, job_id=None, attempt=1)
        self.assertTrue(base.ok and cand.ok, (base.detail, cand.detail))
        self.assertEqual(base.answer, "The tests should pass.")
        self.assertEqual(cand.answer, "Ran 3 tests: OK (observed).")
        self.assertEqual(base.tool_uses, [])
        self.assertEqual(cand.tool_uses, [{"name": "Bash", "input_summary": '{"command":"python3 -m unittest"}', "is_error": False}])
        self.assertEqual((base.harness_commit, cand.harness_commit), (self.base, self.cand))
        self.assertEqual(base.fixture_digest, cand.fixture_digest)
        self.assertEqual(cand.model, "claude-fake-1")
        self.assertEqual(cand.model_requested, "claude-fake-1")

        calls = self.fake_calls()
        self.assertEqual([c["cwd"] for c in calls], [a.path, b.path])
        self.assertNotEqual(calls[0]["claude_md"], calls[1]["claude_md"])
        self.assertIn(MARKER, calls[1]["claude_md"])
        for call in calls:
            argv = call["argv"]
            self.assertNotIn("--safe-mode", argv)
            self.assertEqual(_flag_values(argv, "--setting-sources"), ["project"])
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(_flag_values(argv, "--permission-mode"), ["dontAsk"])
            self.assertEqual(_flag_values(argv, "--output-format"), ["stream-json"])
            self.assertEqual(_flag_values(argv, "--tools"), ["Read,Grep,Edit,Bash"])
            self.assertEqual(_allowed(argv), ["Read", "Grep", "Edit", "Bash(python3 -m unittest*)"])
            self.assertEqual(json.loads(_flag_values(argv, "--settings")[0]), {"autoMemoryEnabled": False})
            self.assertEqual(call["stdin"], "Run the tests and tell me if they pass.")
            self.assertEqual(call["env"]["AUTOTUNE_ORIGIN"], "replay")
            self.assertFalse(call["env"]["ANTHROPIC_API_KEY"])
        self.assertEqual(_flag_values(calls[1]["argv"], "--model"), ["claude-fake-1"])

        env = cand.environment
        self.assertEqual(env["cli_build"], "9.9.9 (Claude Code)")
        self.assertEqual(env["model_requested"], "claude-fake-1")
        self.assertEqual(env["model_actual"], "claude-fake-1")
        self.assertEqual(env["allowed_bash"], ["python3 -m unittest*"])
        self.assertEqual(env["timeout_seconds"], 30.0)
        self.assertFalse(env["ancestor_customization_present"])
        self.assertTrue(env["settings_flags"]["auto_memory_disabled_requested"])
        self.assertEqual(env["harness_commit"], self.cand)
        for key in ("os", "python", "git", "tools", "ancestor_customizations", "argv"):
            self.assertIn(key, env)
        version_calls = [r for r in self.runs if r[1:] == ["--version"]]
        self.assertEqual(len(version_calls), 1, "cli build is cached per job")

    def test_invalid_bash_pattern_is_invalid_case_without_a_call(self):
        a, _ = self.both(_case())
        case = _case(allowed_bash=["ls\nrm -rf /"])
        result = replay.run_arm(self.adapter, a, case, "baseline", None, make_cfg(), conn_factory=self.conn_factory, job_id=None, attempt=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_case")
        self.assertFalse(self.log_path.exists())
        conn = self.conn_factory()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 0)
        finally:
            conn.close()

    def test_timeout_reported_as_error_code(self):
        self.script_path.write_text(json.dumps({"responses": [{"sleep": 5, "result": "late"}]}), encoding="utf-8")
        a, _ = self.both(_case(timeout_seconds=0.5))
        result = replay.run_arm(self.adapter, a, _case(timeout_seconds=0.5), "baseline", None, make_cfg(), conn_factory=self.conn_factory, job_id=None, attempt=1)
        self.assertEqual(result.error_code, "timeout")
        self.assertIsNone(result.answer)
