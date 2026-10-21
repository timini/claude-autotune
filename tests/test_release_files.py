"""Static checks of the release files: the plugin eval suite and the CI workflows.

Nothing here runs a model or the eval suite. The eval format is from the Claude Code 2.1.306 documentation; these
tests keep the cases inside it (unknown frontmatter keys are an error there) and keep paid jobs off pull requests.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
WORKFLOWS = ROOT / ".github" / "workflows"

PROMPT_KEYS = {
    "schema_version", "name", "description", "expected_outcome", "tags", "plugins", "runs", "model",
    "max_turns", "timeout_seconds", "allowed_tools", "append_system_prompt", "env",
}
GRADER_FIELDS = {
    "regex": {"pattern", "flags", "match", "target"},
    "tool_used": {"tool", "input_match", "min", "max"},
    "tool_order": {"before", "after"},
    "file_exists": {"path", "exists"},
    "llm": {"criteria", "focus"},
    "baseline": {"baseline_file", "criteria"},
}
REQUIRED_GRADER_FIELDS = {"regex": {"pattern"}, "tool_used": {"tool"}, "file_exists": {"path"}}
MATCH = re.compile(r"^(contains|not_contains|count:\d+)$")
TARGETS = {"last_message", "trace", "files", "mock_calls"}


def frontmatter(path: Path) -> dict[str, str]:
    """Top-level `key: value` pairs of a `---` delimited block. Values are left as raw text."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{path} has no frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError(f"{path} frontmatter is not closed")
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise AssertionError(f"{path}: not a key line: {line!r}")
        if key in out:
            raise AssertionError(f"{path}: duplicate key {key}")
        out[key.strip()] = value.strip()
    return out


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def cases() -> list[Path]:
    return sorted(p for p in EVALS.iterdir() if p.is_dir() and p.name != "results")


class EvalSuiteTests(unittest.TestCase):
    def test_suite_has_two_or_three_cases(self):
        self.assertIn(len(cases()), (2, 3))

    def test_prompt_frontmatter_uses_documented_keys(self):
        for case in cases():
            with self.subTest(case=case.name):
                fm = frontmatter(case / "prompt.md")
                self.assertLessEqual(set(fm), PROMPT_KEYS)
                self.assertEqual(unquote(fm["schema_version"]), "1.1")
                self.assertEqual(fm["name"], case.name)
                runs = int(fm.get("runs", "3"))
                self.assertTrue(1 <= runs <= 50)
                body = (case / "prompt.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]
                self.assertTrue(body.strip(), "empty prompt body")

    def test_each_case_has_valid_graders(self):
        for case in cases():
            graders = sorted((case / "graders").glob("*.md"))
            with self.subTest(case=case.name):
                self.assertTrue(graders, "a case needs at least one grader")
            for grader in graders:
                with self.subTest(grader=f"{case.name}/{grader.name}"):
                    fm = frontmatter(grader)
                    kind = fm.get("type")
                    self.assertIn(kind, GRADER_FIELDS)
                    self.assertLessEqual(set(fm) - {"type"}, GRADER_FIELDS[kind])
                    self.assertLessEqual(REQUIRED_GRADER_FIELDS.get(kind, set()), set(fm))
                    if "match" in fm:
                        self.assertRegex(fm["match"], MATCH)
                    if "target" in fm:
                        self.assertIn(fm["target"], TARGETS)
                    if kind == "regex":
                        re.compile(unquote(fm["pattern"]))

    def test_normal_response_checks_the_mod_adds_no_text(self):
        fms = [frontmatter(g) for g in (EVALS / "normal-response" / "graders").glob("*.md")]
        self.assertIn(("autotune", "not_contains"), [(unquote(f.get("pattern", "")), f.get("match")) for f in fms])

    def test_results_are_ignored_by_git(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("evals/results/", ignored)


def top_level_block(text: str, key: str) -> str:
    """The lines of a top-level YAML mapping entry, up to the next top-level key."""
    m = re.search(rf"^{re.escape(key)}:.*?(?=^\S)", text + "\nend:\n", re.M | re.S)
    if m is None:
        raise AssertionError(f"no top-level {key}")
    return m.group(0)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        self.paid = (WORKFLOWS / "paid-tests.yml").read_text(encoding="utf-8")

    def test_ci_runs_on_push_and_pull_request_without_secrets(self):
        on = top_level_block(self.ci, "on")
        self.assertIn("pull_request", on)
        self.assertIn("push", on)
        self.assertNotIn("secrets.", self.ci)
        self.assertNotIn("environment:", self.ci)

    def test_ci_matrix_and_steps(self):
        self.assertRegex(self.ci, r"os: \[ubuntu-latest, macos-latest\]")
        self.assertRegex(self.ci, r'python: \["3\.11", "3\.13"\]')
        for command in (
            "python3 -m unittest discover -s tests -t .",
            "npm install -g @anthropic-ai/claude-code@2.1.306",
            "claude plugin validate .",
            "claude plugin test .",
        ):
            self.assertIn(command, self.ci)
        self.assertNotIn("plugin eval", self.ci)

    def test_paid_tests_are_manual_and_protected(self):
        on = top_level_block(self.paid, "on")
        self.assertIn("workflow_dispatch", on)
        for trigger in ("pull_request", "push", "schedule", "workflow_run"):
            self.assertNotIn(trigger, on)
        self.assertRegex(self.paid, r"(?m)^\s+environment: paid-tests$")
        self.assertIn("--no-publish", self.paid)
        self.assertIn("--budget-calls", self.paid)

    def test_no_em_or_en_dashes_in_release_files(self):
        files = [WORKFLOWS / "ci.yml", WORKFLOWS / "paid-tests.yml", ROOT / "docs" / "OPERATIONS.md",
                 ROOT / "docs" / "RELEASE_EVIDENCE.md", *EVALS.rglob("*.md")]
        for path in files:
            if "results" in path.relative_to(ROOT).parts:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(chr(0x2014), text)
                self.assertNotIn(chr(0x2013), text)


if __name__ == "__main__":
    unittest.main()
