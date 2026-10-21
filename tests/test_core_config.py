import json
import tempfile
import unittest
from pathlib import Path

from autotune import config
from autotune.config import DEFAULTS, ConfigError, config_digest, glob_match, load_config


class ConfigTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, data):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.root / ".autotune.json").write_text(text, encoding="utf-8")

    def assertRejected(self, data, fragment=None):
        self.write(data)
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.root)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))


class DefaultsTest(ConfigTestBase):
    def test_absent_file_means_not_enrolled(self):
        self.assertIsNone(load_config(self.root))

    def test_empty_object_yields_defaults(self):
        self.write({})
        self.assertEqual(load_config(self.root), DEFAULTS)

    def test_defaults_are_safe(self):
        self.assertIs(DEFAULTS["automatic_apply"], False)
        self.assertEqual(DEFAULTS["executable_paths"], [])
        self.assertEqual(DEFAULTS["worker_idle_seconds"], 30)
        self.assertEqual(DEFAULTS["lease_seconds"], 300)
        self.assertEqual(DEFAULTS["max_attempts"], 5)

    def test_defaults_pass_validation(self):
        self.write(dict(DEFAULTS))
        self.assertEqual(load_config(self.root), DEFAULTS)

    def test_overrides_merge_onto_defaults(self):
        self.write({"enabled": False, "editable_paths": ["docs/**/*.md"]})
        cfg = load_config(self.root)
        self.assertIs(cfg["enabled"], False)
        self.assertEqual(cfg["editable_paths"], ["docs/**/*.md"])
        self.assertEqual(cfg["lease_seconds"], DEFAULTS["lease_seconds"])

    def test_result_is_independent_of_defaults(self):
        self.write({})
        cfg = load_config(self.root)
        cfg["editable_paths"].append("x")
        cfg["enabled"] = False
        again = load_config(self.root)
        self.assertNotIn("x", DEFAULTS["editable_paths"])
        self.assertIs(DEFAULTS["enabled"], True)
        self.assertEqual(again, DEFAULTS)


class RejectionTest(ConfigTestBase):
    def test_unknown_key(self):
        self.assertRejected({"enabled": True, "grant_everything": True}, "grant_everything")

    def test_malformed_json(self):
        self.assertRejected("{not json")

    def test_non_object(self):
        self.assertRejected("[]")

    def test_duplicate_keys(self):
        self.assertRejected('{"enabled": true, "enabled": false}', "enabled")

    def test_nan_constant(self):
        self.assertRejected('{"lease_seconds": NaN}')

    def test_directory_in_place_of_file(self):
        (self.root / ".autotune.json").mkdir()
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_bool_fields_reject_ints_and_strings(self):
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                self.assertRejected({"enabled": value}, "enabled")

    def test_int_fields_reject_bools_floats_and_non_positive(self):
        for value in (True, False, 1.5, "5", 0, -1, None):
            with self.subTest(value=value):
                self.assertRejected({"max_attempts": value}, "max_attempts")

    def test_string_fields_reject_empty(self):
        self.assertRejected({"trigger": ""}, "trigger")
        self.assertRejected({"provider": 3}, "provider")

    def test_path_lists_must_be_lists_of_strings(self):
        self.assertRejected({"editable_paths": "CLAUDE.md"}, "editable_paths")
        self.assertRejected({"executable_paths": [1]}, "executable_paths")


class GlobValidationTest(ConfigTestBase):
    def test_invalid_syntax_rejected_in_both_lists(self):
        bad = [
            "",
            "/etc/passwd",
            "/CLAUDE.md",
            "../outside",
            "docs/../../outside",
            "docs/..",
            "a//b",
            "./CLAUDE.md",
            "docs/",
            "docs\\x.md",
        ]
        for key in ("editable_paths", "executable_paths"):
            for pattern in bad:
                with self.subTest(key=key, pattern=pattern):
                    self.assertRejected({key: [pattern]}, key)

    def test_reserved_paths_cannot_be_editable(self):
        reserved = [
            ".autotune.json",
            ".git/**",
            ".git/config",
            ".git",
            ".autotune/**",
            ".autotune/evaluator.json",
            ".autotune",
            "**",
            "**/*.json",
            "**/CLAUDE.md",
            "*",
            ".*",
            ".a*/**",
            ".g?t/hooks/*",
            "*.json",
        ]
        for pattern in reserved:
            with self.subTest(pattern=pattern):
                self.assertRejected({"editable_paths": [pattern]}, "reserved")

    def test_reserved_path_hidden_among_valid_entries(self):
        self.assertRejected({"editable_paths": ["CLAUDE.md", ".git/hooks/pre-commit"]}, "reserved")

    def test_ordinary_globs_accepted(self):
        good = [
            "CLAUDE.md",
            ".claude/rules/**",
            ".claude/skills/*/SKILL.md",
            "docs/**/*.md",
            "*.md",
            ".autotune-notes/**",
            "src/.git-keep",
        ]
        self.write({"editable_paths": good, "executable_paths": ["scripts/check.sh"]})
        cfg = load_config(self.root)
        self.assertEqual(cfg["editable_paths"], good)

    def test_every_accepted_pattern_misses_reserved_samples(self):
        samples = [
            ".autotune.json",
            ".git",
            ".git/config",
            ".git/CLAUDE.md",
            ".git/hooks/pre-commit",
            ".autotune",
            ".autotune/evaluator.json",
            ".autotune/x/CLAUDE.md",
        ]
        candidates = [
            "CLAUDE.md", ".claude/rules/**", "docs/**/*.md", "*.md", "**",
            "**/*.md", "*", ".*/**", ".git/**", "*/*", "?git/*", "[.]autotune.json",
        ]
        for pattern in candidates:
            if config.may_match_reserved(pattern):
                continue
            for sample in samples:
                with self.subTest(pattern=pattern, sample=sample):
                    self.assertFalse(glob_match(pattern, sample))


class GlobMatchTest(unittest.TestCase):
    def test_semantics(self):
        self.assertTrue(glob_match(".claude/rules/**", ".claude/rules/a/b.md"))
        self.assertTrue(glob_match(".claude/rules/**", ".claude/rules"))
        self.assertTrue(glob_match("docs/**/*.md", "docs/a.md"))
        self.assertTrue(glob_match("docs/**/*.md", "docs/x/y/a.md"))
        self.assertFalse(glob_match("*.md", "docs/a.md"))
        self.assertFalse(glob_match("CLAUDE.md", "claude.md"))
        self.assertTrue(glob_match("*", ".hidden"))


class DigestTest(ConfigTestBase):
    def test_digest_is_sha256_hex(self):
        self.assertRegex(config_digest(DEFAULTS), r"^[0-9a-f]{64}$")

    def test_equivalent_configs_share_digest(self):
        self.write({})
        implicit = load_config(self.root)
        reordered = dict(reversed(list(DEFAULTS.items())))
        self.write(json.dumps(reordered, indent=4))
        explicit = load_config(self.root)
        self.assertEqual(config_digest(implicit), config_digest(explicit))
        self.assertEqual(config_digest(implicit), config_digest(DEFAULTS))

    def test_digest_stable_across_calls(self):
        cfg = dict(DEFAULTS)
        self.assertEqual(config_digest(cfg), config_digest(cfg))

    def test_any_value_change_changes_digest(self):
        base = config_digest(DEFAULTS)
        self.write({"max_attempts": 6})
        self.assertNotEqual(config_digest(load_config(self.root)), base)
        self.write({"editable_paths": list(reversed(DEFAULTS["editable_paths"]))})
        self.assertNotEqual(config_digest(load_config(self.root)), base)


if __name__ == "__main__":
    unittest.main()


class ReservedCaseTest(ConfigTestBase):
    def test_case_variants_of_reserved_paths_are_rejected(self):
        for pattern in (".GIT/hooks/*", ".Git/config", ".AUTOTUNE.JSON", ".Autotune/x", ".GIT"):
            with self.subTest(pattern=pattern):
                self.assertRejected({"editable_paths": [pattern]}, "reserved")

    def test_executable_paths_cannot_name_reserved_paths(self):
        self.assertRejected({"executable_paths": [".git/hooks/pre-commit"]}, "reserved")

    def test_whole_file_settings_and_mcp_globs_are_rejected(self):
        for pattern in (".claude/settings.json", ".claude/settings.local.json", ".mcp.json",
                        ".claude/*.json", ".claude/**", ".CLAUDE/Settings.json", ".claude/settings*.json"):
            with self.subTest(pattern=pattern):
                self.assertRejected({"editable_paths": [pattern]}, "field-level")

    def test_defaults_still_accepted(self):
        self.write({"editable_paths": ["CLAUDE.md", ".claude/skills/**", "docs/**/*.md"]})
        self.assertIsNotNone(load_config(self.root))
