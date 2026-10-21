"""Cycle 1 config keys: models, CLI, limits, protected paths and field rules."""

from __future__ import annotations

from autotune import config
from autotune.config import ConfigError, load_config

from tests.test_core_config import ConfigTestBase

SETTINGS = ".claude/settings.json"


class NewKeysTest(ConfigTestBase):
    def load(self, data):
        self.write(data)
        return load_config(self.root)

    def test_defaults(self):
        cfg = self.load({})
        self.assertIsNone(cfg["reviewer_model"])
        self.assertEqual(cfg["claude_executable"], "claude")
        self.assertFalse(cfg["allow_api_key"])
        self.assertEqual((cfg["review_timeout_seconds"], cfg["author_timeout_seconds"],
                          cfg["review_input_chars"]), (300, 600, 60000))
        self.assertEqual((cfg["max_data_mb"], cfg["log_max_bytes"], cfg["log_keep"]), (1024, 5242880, 2))
        self.assertEqual(cfg["protected_paths"], [])
        self.assertEqual(cfg["field_rules"], {})

    def test_model_keys_take_a_string_or_null(self):
        for key in ("reviewer_model", "author_model", "judge_model", "curator_model", "replay_model"):
            with self.subTest(key=key):
                self.assertEqual(self.load({key: "claude-haiku-5-5"})[key], "claude-haiku-5-5")
                self.assertIsNone(self.load({key: None})[key])
                self.assertRejected({key: ""}, key)
                self.assertRejected({key: 3}, key)

    def test_scalar_types_are_checked(self):
        self.assertRejected({"allow_api_key": "yes"}, "allow_api_key")
        self.assertRejected({"claude_executable": ""}, "claude_executable")
        self.assertRejected({"claude_executable": None}, "claude_executable")
        for key in ("review_timeout_seconds", "author_timeout_seconds", "review_input_chars",
                    "max_data_mb", "log_max_bytes", "log_keep"):
            with self.subTest(key=key):
                self.assertRejected({key: 0}, key)
                self.assertRejected({key: True}, key)

    def test_reviewer_key_from_m1_is_not_needed_or_accepted(self):
        self.assertNotIn("reviewer", config.DEFAULTS)
        self.assertRejected({"reviewer": "x"}, "unknown keys")

    def test_protected_paths_are_validated_globs(self):
        self.assertEqual(self.load({"protected_paths": ["src/**", "Makefile"]})["protected_paths"],
                         ["src/**", "Makefile"])
        self.assertRejected({"protected_paths": "src/**"}, "protected_paths")
        self.assertRejected({"protected_paths": ["/abs"]}, "relative")
        self.assertRejected({"protected_paths": ["a/../b"]}, "protected_paths")

    def test_always_protected_set(self):
        self.assertEqual(set(config.always_protected()),
                         {".autotune.json", ".autotune/**", ".git/**", "**/.gitmodules", "**/.gitattributes"})


class FieldRulesTest(ConfigTestBase):
    def rules(self, rules):
        self.write({"field_rules": rules})
        return load_config(self.root)["field_rules"]

    def test_valid_rules_are_kept(self):
        rules = {SETTINGS: {"editable_keys": ["permissions.allow", "hooks"], "executable_keys": ["hooks.Stop"]},
                 ".mcp.json": {"editable_keys": ["mcpServers.docs"]}}
        self.assertEqual(self.rules(rules), rules)

    def test_file_must_be_a_settings_or_mcp_file(self):
        self.assertRejected({"field_rules": {"package.json": {"editable_keys": ["scripts"]}}}, "must be one of")
        self.assertRejected({"field_rules": {".CLAUDE/settings.json": {"editable_keys": ["x"]}}}, "must be one of")

    def test_shapes_are_checked(self):
        self.assertRejected({"field_rules": []}, "field_rules")
        self.assertRejected({"field_rules": {SETTINGS: ["hooks"]}}, "must be an object")
        self.assertRejected({"field_rules": {SETTINGS: {"editable_keys": "hooks"}}}, "list")
        self.assertRejected({"field_rules": {SETTINGS: {"editable": ["hooks"]}}}, "unknown keys")

    def test_keys_must_be_dotted_names(self):
        for bad in ("", "a..b", ".a", "a.", "a b", "a/b", "hooks\n", 5, None):
            with self.subTest(key=bad):
                self.assertRejected({"field_rules": {SETTINGS: {"editable_keys": [bad]}}}, "dotted key")
        self.assertEqual(self.rules({SETTINGS: {"editable_keys": ["env.$HOME_x-1"]}})[SETTINGS]["editable_keys"],
                         ["env.$HOME_x-1"])

    def test_at_most_fifty_keys_per_list(self):
        keys = [f"k{i}" for i in range(51)]
        self.assertRejected({"field_rules": {SETTINGS: {"editable_keys": keys}}}, "at most 50")
        self.assertEqual(len(self.rules({SETTINGS: {"editable_keys": keys[:50]}})[SETTINGS]["editable_keys"]), 50)

    def test_executable_keys_must_be_covered_by_editable_keys(self):
        self.assertRejected({"field_rules": {SETTINGS: {"executable_keys": ["hooks"]}}}, "not covered")
        self.assertRejected({"field_rules": {SETTINGS: {"editable_keys": ["hooks.Stop"],
                                                        "executable_keys": ["hooks"]}}}, "not covered")
        # Prefixes cover whole segments only.
        self.assertRejected({"field_rules": {SETTINGS: {"editable_keys": ["hook"],
                                                        "executable_keys": ["hooks"]}}}, "not covered")
        self.rules({SETTINGS: {"editable_keys": ["hooks"], "executable_keys": ["hooks.Stop.0"]}})

    def test_whole_file_globs_stay_rejected_with_field_rules(self):
        self.assertRejected({"editable_paths": [SETTINGS], "field_rules": {SETTINGS: {"editable_keys": ["x"]}}},
                            "field-level")

    def test_key_covers(self):
        self.assertTrue(config.key_covers("hooks", "hooks"))
        self.assertTrue(config.key_covers("hooks", "hooks.Stop"))
        self.assertFalse(config.key_covers("hooks.Stop", "hooks"))
        self.assertFalse(config.key_covers("hook", "hooks"))
