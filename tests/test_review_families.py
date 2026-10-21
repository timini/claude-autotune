"""The reviewer is told the task families of the committed cases, so its
task_family can match them exactly."""

from __future__ import annotations

import json

from autotune import review
from tests._cycle2_support import FAMILY, Cycle2Case, case_obj, git


class KnownTaskFamiliesTest(Cycle2Case):
    def test_families_come_from_cases_committed_at_head(self):
        facts = {"toplevel": str(self.repo), "head": self.base}
        self.assertEqual(review.known_task_families(facts), [FAMILY])

        extra = case_obj("target-docs", "target", [{"id": "a", "type": "answer_contains", "text": "x"}],
                         family="docs-update")
        (self.repo / ".autotune/cases/target-docs.json").write_text(json.dumps(extra))
        self.assertEqual(review.known_task_families(facts), [FAMILY], "the working tree is not read")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "add case")
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(review.known_task_families({"toplevel": str(self.repo), "head": head}),
                         ["docs-update", FAMILY])

    def test_missing_facts_or_bad_head_give_an_empty_list(self):
        self.assertEqual(review.known_task_families({}), [])
        self.assertEqual(review.known_task_families({"toplevel": str(self.repo), "head": "0" * 40}), [])
        self.assertEqual(review.known_task_families({"toplevel": str(self.root / "nope"), "head": self.base}), [])
