"""Config keys for skill consolidation (cycle 3)."""

from __future__ import annotations

import json
import unittest

from autotune.config import DEFAULTS, ConfigError, parse_config


class SkillConfigTests(unittest.TestCase):
    def parse(self, **keys) -> dict:
        return parse_config(json.dumps(keys))

    def test_defaults(self):
        cfg = self.parse()
        self.assertEqual((cfg["pinned_skills"], cfg["critical_skills"]), ([], []))
        self.assertEqual(cfg["consolidation_overlap_threshold"], 0.45)
        self.assertEqual(DEFAULTS["consolidation_overlap_threshold"], 0.45)

    def test_skill_globs_accepted(self):
        cfg = self.parse(pinned_skills=[".claude/skills/release/**"], critical_skills=[".claude/skills/*"])
        self.assertEqual(cfg["critical_skills"], [".claude/skills/*"])

    def test_skill_globs_rejected(self):
        for bad in ("x", [""], ["/abs"], ["a/../b"], [3], ["dir/"]):
            for key in ("pinned_skills", "critical_skills"):
                with self.subTest(key=key, value=bad), self.assertRaises(ConfigError):
                    self.parse(**{key: bad})

    def test_threshold_bounds(self):
        for good in (0, 1, 0.0, 0.7, 1.0):
            self.assertEqual(self.parse(consolidation_overlap_threshold=good)["consolidation_overlap_threshold"], good)
        for bad in (-0.01, 1.01, True, "0.5", None, [0.5]):
            with self.subTest(value=bad), self.assertRaises(ConfigError):
                self.parse(consolidation_overlap_threshold=bad)


if __name__ == "__main__":
    unittest.main()
