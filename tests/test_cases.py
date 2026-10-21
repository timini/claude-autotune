"""Case manifests: committed-base loading, validation, selection, evaluator identity."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from autotune import __version__, cases, git_ops, jsonschema_lite
from tests._git_support import GitRepoTestCase, git, write

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((ROOT / "examples" / "replay-case.json").read_text(encoding="utf-8"))


def manifest(cid: str, **overrides) -> dict:
    obj = {
        "id": cid,
        "task_family": "verification-reporting",
        "kind": "target",
        "critical": True,
        "description": "d",
        "request": "do the thing",
        "fixture": {"files": {"a.txt": "hello\n"}, "from_commit": None},
        "harness_paths": ["CLAUDE.md"],
        "allowed_tools": ["Read"],
        "allowed_bash": [],
        "network": "not_required",
        "trials_per_arm": 1,
        "timeout_seconds": 60,
        "graders": [{"type": "answer_contains", "text": "hello"}],
    }
    obj.update(overrides)
    return obj


def make_case(cid: str, **overrides) -> cases.Case:
    case, errors = cases.validate_case(manifest(cid, **overrides), stem=cid)
    assert case is not None, errors
    return case


class ExampleTest(unittest.TestCase):
    def test_example_is_valid_against_schema_and_loader(self):
        schema = json.loads((ROOT / "schemas" / "case.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(jsonschema_lite.validate(EXAMPLE, schema), [])
        case, errors = cases.validate_case(EXAMPLE, stem="report-observed-test-status")
        self.assertEqual(errors, [])
        self.assertEqual(case.kind, "target")
        self.assertIsNone(case.judge)

    def test_normalization_fills_grader_defaults(self):
        case = make_case(
            "norm-case",
            critical=False,
            graders=[
                {"type": "answer_contains", "text": "x"},
                {"type": "command", "argv": ["true"], "test_format": "pytest"},
                {"id": "explicit", "type": "file_absent", "path": "b.txt", "critical": True},
            ],
            judge={"rubric": "r"},
        )
        g0, g1, g2 = case.graders
        self.assertEqual((g0["id"], g0["critical"], g0["case_sensitive"]), ("answer-contains-1", False, False))
        self.assertEqual((g1["expect_exit"], g1["min_tests"], g1["timeout_seconds"]), (0, 1, 120))
        self.assertEqual((g2["id"], g2["critical"]), ("explicit", True))
        self.assertEqual(case.judge, {"rubric": "r", "required": False})
        self.assertEqual(case.to_dict()["graders"][0]["id"], "answer-contains-1")


class ValidationTest(unittest.TestCase):
    def assert_invalid(self, obj, code, *, stem=None):
        case, errors = cases.validate_case(obj, path="p.json", stem=stem or obj.get("id"))
        self.assertIsNone(case)
        self.assertIn(code, [e["code"] for e in errors], errors)
        return errors

    def test_rejections(self):
        bad = [
            (manifest("x-case", kind="other"), "schema"),
            (manifest("x-case", trials_per_arm=6), "schema"),
            (manifest("x-case", timeout_seconds=10), "schema"),
            (manifest("x-case", extra=1), "schema"),
            (manifest("x-case", graders=[]), "schema"),
            (manifest("x-case", graders=[{"type": "answer_contains"}]), "schema"),
            (manifest("x-case", graders=[{"type": "answer_contains", "text": "a", "path": "b"}]), "schema"),
            (manifest("x-case", graders=[{"type": "nope"}]), "schema"),
            (manifest("x-case", allowed_bash=["rm (x)"], allowed_tools=["Bash"]), "schema"),
            (manifest("X_case"), "schema"),
            (manifest("x-case", fixture={"files": {"../escape": "x"}}), "unsafe_path"),
            (manifest("x-case", fixture={"files": {"/abs": "x"}}), "unsafe_path"),
            (manifest("x-case", fixture={"files": {".git/config": "x"}}), "unsafe_path"),
            (manifest("x-case", harness_paths=["../CLAUDE.md"]), "unsafe_path"),
            (manifest("x-case", allowed_bash=["ls*"]), "tools"),
            (manifest("x-case", graders=[{"type": "answer_matches", "pattern": "("}]), "grader"),
            (manifest("x-case", graders=[{"id": "a1", "type": "tool_used", "tool": "Read"},
                                         {"id": "a1", "type": "tool_used", "tool": "Bash"}]), "grader"),
            (manifest("x-case", graders=[{"type": "file_unchanged", "path": "not-in-fixture"}]), "grader"),
            (manifest("x-case", graders=[{"type": "command", "argv": ["true"], "min_tests": 2}]), "grader"),
        ]
        for obj, code in bad:
            with self.subTest(code=code, obj=obj):
                self.assert_invalid(obj, code)

    def test_id_must_match_stem(self):
        self.assert_invalid(manifest("x-case"), "id_mismatch", stem="other-case")

    def test_file_unchanged_from_commit_path_is_allowed(self):
        case, errors = cases.validate_case(
            manifest("x-case", fixture={"from_commit": {"paths": ["src/**"]}},
                     graders=[{"type": "file_unchanged", "path": "src/app.py"}]),
            stem="x-case",
        )
        self.assertEqual(errors, [])
        self.assertEqual(case.fixture, {"files": {}, "from_commit": {"paths": ["src/**"]}})

    def test_parse_rejects_duplicate_keys_and_bad_json(self):
        _, errors = cases.parse_case(b'{"id": "a", "id": "b"}', path="p")
        self.assertEqual(errors[0]["code"], "invalid_json")
        _, errors = cases.parse_case(b"\xff", path="p")
        self.assertEqual(errors[0]["code"], "invalid_json")


class LoadCasesTest(GitRepoTestCase):
    def commit_case(self, name: str, content) -> None:
        text = content if isinstance(content, str) else json.dumps(content)
        write(self.repo, f".autotune/cases/{name}", text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"case {name}")

    def test_no_cases_directory(self):
        self.assertEqual(cases.load_cases(self.repo, self.base), ([], []))

    def test_bad_base_raises(self):
        with self.assertRaises(git_ops.GitError):
            cases.load_cases(self.repo, "0" * 40)

    def test_reads_committed_content_not_working_tree(self):
        self.commit_case("good-case.json", manifest("good-case", description="committed"))
        base = git(self.repo, "rev-parse", "HEAD").strip()
        write(self.repo, ".autotune/cases/good-case.json", json.dumps(manifest("good-case", description="edited")))
        write(self.repo, ".autotune/cases/untracked-case.json", json.dumps(manifest("untracked-case")))
        loaded, errors = cases.load_cases(self.repo, base)
        self.assertEqual(errors, [])
        self.assertEqual([(c.id, c.description) for c in loaded], [("good-case", "committed")])
        self.assertEqual(loaded[0].path, ".autotune/cases/good-case.json")
        self.assertEqual(len(loaded[0].digest), 64)
        # An older base without the case sees none.
        self.assertEqual(cases.load_cases(self.repo, self.base), ([], []))

    def test_invalid_cases_are_reported_not_used(self):
        self.commit_case("good-case.json", manifest("good-case"))
        self.commit_case("mismatch-case.json", manifest("other-name"))
        self.commit_case("broken-case.json", "{not json")
        self.commit_case("schema-case.json", manifest("schema-case", trials_per_arm=9))
        self.commit_case("README.md", "notes")
        base = git(self.repo, "rev-parse", "HEAD").strip()
        loaded, errors = cases.load_cases(self.repo, base)
        self.assertEqual([c.id for c in loaded], ["good-case"])
        by_path = {e["path"]: e["code"] for e in errors}
        self.assertEqual(by_path, {
            ".autotune/cases/broken-case.json": "invalid_json",
            ".autotune/cases/mismatch-case.json": "id_mismatch",
            ".autotune/cases/schema-case.json": "schema",
        })

    def test_symlink_case_is_rejected(self):
        self.commit_case("good-case.json", manifest("good-case"))
        (self.repo / ".autotune/cases/link-case.json").symlink_to("good-case.json")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "link")
        loaded, errors = cases.load_cases(self.repo, git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual([c.id for c in loaded], ["good-case"])
        self.assertEqual([(e["path"], e["code"]) for e in errors], [(".autotune/cases/link-case.json", "unsafe_path")])

    def test_evaluator_identity(self):
        none_yet = cases.evaluator_identity(self.repo, self.base)
        self.assertIsNone(none_yet["cases_tree"])
        self.assertEqual(none_yet["autotune_version"], __version__)
        self.commit_case("good-case.json", manifest("good-case"))
        first = git(self.repo, "rev-parse", "HEAD").strip()
        ident = cases.evaluator_identity(self.repo, first)
        self.assertEqual(ident["cases_tree"], git(self.repo, "rev-parse", f"{first}:.autotune").strip())
        self.assertEqual(ident["autotune_code"], none_yet["autotune_code"])
        self.commit_case("good-case.json", manifest("good-case", description="changed"))
        second = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertNotEqual(cases.evaluator_identity(self.repo, second)["cases_tree"], ident["cases_tree"])

    def test_code_digest_tracks_code_and_prompts(self):
        root = self.tmp / "plugin"
        write(root, "autotune/a.py", "x = 1\n")
        write(root, "prompts/p.md", "prompt\n")
        write(root, "autotune/notes.txt", "ignored\n")
        d1 = cases.code_digest(root)
        write(root, "autotune/notes.txt", "still ignored\n")
        self.assertEqual(cases.code_digest(root), d1)
        write(root, "prompts/p.md", "prompt changed\n")
        d2 = cases.code_digest(root)
        self.assertNotEqual(d2, d1)
        write(root, "autotune/a.py", "x = 2\n")
        self.assertNotEqual(cases.code_digest(root), d2)


class SelectRequiredTest(unittest.TestCase):
    def setUp(self):
        self.cases = [
            make_case("target-one"),
            make_case("target-two"),
            make_case("target-other", task_family="other-family"),
            make_case("gen-one", kind="generalization"),
            make_case("protected-hit", kind="protected", harness_paths=[".claude/rules/**"]),
            make_case("protected-soft", kind="protected", critical=False, harness_paths=["CLAUDE.md"]),
            make_case("protected-miss", kind="protected", harness_paths=["docs/**"]),
            make_case("negative-hit", kind="negative_trigger", critical=False, harness_paths=["CLAUDE.md"]),
            make_case("negative-miss", kind="negative_trigger", harness_paths=["src/**"]),
            make_case("general-b", kind="general", harness_paths=[]),
            make_case("general-a", kind="general", harness_paths=[]),
        ]

    def ids(self, row, **kw):
        return [c.id for c in cases.select_required(self.cases, row, **kw)]

    def test_after_diff(self):
        row = {
            "task_family": "verification-reporting",
            "required_cases_json": "[]",
            "validation_json": json.dumps({"files": ["CLAUDE.md", ".claude/rules/verify.md"]}),
            "allowed_paths_json": json.dumps(["docs/**"]),
        }
        self.assertEqual(self.ids(row), [
            "target-one", "target-two", "gen-one", "protected-hit", "negative-hit", "general-a",
        ])

    def test_preregistered_selection_is_stable(self):
        row = {
            "task_family": "verification-reporting",
            "validation_json": json.dumps({"files": ["CLAUDE.md", ".claude/rules/verify.md"]}),
        }
        first = self.ids(row)
        again = self.ids({**row, "required_cases_json": json.dumps(first)})
        self.assertEqual(again, first)

    def test_nested_git_path_rejected(self):
        case, errors = cases.validate_case(manifest("x-case", fixture={"files": {"sub/.git/config": "x"}}), stem="x-case")
        self.assertIsNone(case)
        self.assertEqual(errors[0]["code"], "unsafe_path")

    def test_propose_time_uses_allowed_globs(self):
        row = {"task_family": None, "validation_json": "{}", "allowed_paths_json": json.dumps([".claude/rules/**"])}
        self.assertEqual(self.ids(row), ["protected-hit", "general-a"])

    def test_pinned_ids_always_included(self):
        row = {
            "task_family": None,
            "required_cases_json": json.dumps(["target-other", "protected-soft", "general-b", "ghost"]),
            "allowed_paths_json": "[]",
        }
        self.assertEqual(self.ids(row), ["target-other", "protected-soft", "general-b"])
        self.assertEqual(cases.missing_pinned(self.cases, row), ["ghost"])

    def test_explicit_paths_and_order_is_deterministic(self):
        row = {"task_family": "verification-reporting"}
        first = self.ids(row, paths=["src/main.py"])
        self.assertEqual(first, ["target-one", "target-two", "gen-one", "negative-miss", "general-a"])
        shuffled = list(reversed(copy.copy(self.cases)))
        self.assertEqual([c.id for c in cases.select_required(shuffled, row, paths=["src/main.py"])], first)


if __name__ == "__main__":
    unittest.main()
