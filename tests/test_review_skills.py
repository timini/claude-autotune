"""Skill usage capture and consolidation triggers in the review commit path.

The skills module is replaced by a stub that follows the cycle 3 interface
(record_episode, inventory, overlaps), so these tests check the wiring:
what is recorded and queued, and that no failure in it costs the review.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from types import SimpleNamespace
from unittest import mock

import autotune
from autotune import review, store
from autotune.util import canonical_json, iso

from tests.test_review import ReviewCase, ev, make_review


def skill_ev(name: str) -> dict:
    return ev("skill_load", canonical_json({"skill": name, "path": f"/somewhere/.claude/skills/{name}/SKILL.md"}))


_MISSING = object()


class StubSkills(types.ModuleType):
    def __init__(self, pairs=(), *, fail_record=False, fail_inventory=False) -> None:
        super().__init__("autotune.skills")
        self.pairs = list(pairs)
        self.fail_record = fail_record
        self.fail_inventory = fail_inventory
        self.inventory_roots = []

    def record_episode(self, conn, episode_id, now):
        for r in conn.execute("SELECT content FROM evidence WHERE episode_id = ? AND kind = 'skill_load' "
                              "ORDER BY position", (episode_id,)):
            item = json.loads(r["content"])
            conn.execute("INSERT INTO skill_events (episode_id, skill, path, event, occurred_at) "
                         "VALUES (?, ?, ?, 'load', ?)", (episode_id, item["skill"], item["path"], iso(now)))
        if self.fail_record:
            raise RuntimeError("record failed after a partial write")

    def inventory(self, root):
        self.inventory_roots.append(root)
        if self.fail_inventory:
            raise OSError("cannot read skills")
        return [SimpleNamespace(name=n, path=f".claude/skills/{n}") for n in ("alpha", "beta", "gamma")]

    def overlaps(self, skills, threshold):
        by_name = {s.name: s for s in skills}
        return [(by_name[a], by_name[b], score, ["shared words"]) for a, b, score in self.pairs if score > threshold]


class ReviewSkillsTests(ReviewCase):
    def setUp(self) -> None:
        super().setUp()
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                           cwd=self.tmp, check=True, capture_output=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tmp, check=True, capture_output=True,
                                   text=True).stdout.strip()

    def use(self, module) -> None:
        """Make `from . import skills` return `module`; None makes it fail."""
        p = mock.patch.dict(sys.modules, {"autotune.skills": module})
        p.start()
        self.addCleanup(p.stop)
        saved = autotune.__dict__.get("skills", _MISSING)
        if module is None:
            autotune.__dict__.pop("skills", None)
        else:
            autotune.skills = module

        def restore():
            if saved is _MISSING:
                autotune.__dict__.pop("skills", None)
            else:
                autotune.skills = saved
        self.addCleanup(restore)

    def review_episode(self, evidence) -> str:
        ep = self.ingest(evidence)
        self.script({"structured_output": make_review(ep)})
        res, state = self.run_review(ep)
        self.assertEqual(state, "succeeded")
        [row] = self.reviews(ep)
        self.assertEqual(row["status"], "valid")
        return ep

    def evidence(self, *skills, tool_calls=0):
        items = [ev("user_request", "Update the greeting and run the focused test.")]
        items += [ev("tool_call", json.dumps({"file_path": f"src/f{i}.py"}), tool="Edit") for i in range(tool_calls)]
        items += [ev("tool_result", "ran 1 test; 1 failure; exit code 1", tool="Bash", is_error=True),
                  ev("assistant_answer", "Updated the greeting. The focused test passes.")]
        items += [skill_ev(s) for s in skills]
        items.append(ev("end_reason", "answer"))
        return tuple(items)

    def consolidate_jobs(self):
        return self.conn.execute("SELECT * FROM jobs WHERE job_type = 'consolidate' ORDER BY id").fetchall()

    def skill_events(self, ep):
        return self.conn.execute("SELECT skill, event FROM skill_events WHERE episode_id = ? ORDER BY id",
                                 (ep,)).fetchall()

    def test_loaded_overlapping_pair_records_loads_and_queues_pair_consolidation(self):
        self.use(StubSkills([("beta", "alpha", 0.8)]))
        ep = self.review_episode(self.evidence("alpha", "beta"))
        self.assertEqual([(r["skill"], r["event"]) for r in self.skill_events(ep)],
                         [("alpha", "load"), ("beta", "load")])
        [job] = self.consolidate_jobs()
        self.assertEqual(job["dedup_key"],
                         f"consolidate:pair:.claude/skills/alpha:.claude/skills/beta:{self.head}")
        payload = json.loads(job["payload_json"])
        self.assertEqual(payload["mode"], "pair")
        self.assertEqual(payload["skills"], [".claude/skills/alpha", ".claude/skills/beta"])
        self.assertEqual(payload["base"], self.head)

    def test_pair_not_loaded_together_or_below_threshold_queues_nothing(self):
        stub = StubSkills([("alpha", "beta", 0.8), ("alpha", "gamma", 0.3)])
        self.use(stub)
        self.review_episode(self.evidence("alpha", "gamma"))
        self.assertEqual(self.consolidate_jobs(), [])

    def test_one_loaded_skill_reads_no_inventory(self):
        stub = StubSkills([("alpha", "beta", 0.9)])
        self.use(stub)
        ep = self.review_episode(self.evidence("alpha"))
        self.assertEqual(stub.inventory_roots, [])
        self.assertEqual(len(self.skill_events(ep)), 1)
        self.assertEqual(self.consolidate_jobs(), [])

    def test_record_failure_rolls_back_skill_rows_but_keeps_the_review(self):
        self.use(StubSkills([("alpha", "beta", 0.8)], fail_record=True))
        ep = self.review_episode(self.evidence("alpha", "beta"))
        self.assertEqual(self.skill_events(ep), [])
        self.assertEqual(self.consolidate_jobs(), [])
        self.assertEqual(self.episode_status(ep), "reviewed")

    def test_inventory_failure_still_records_loads(self):
        self.use(StubSkills([("alpha", "beta", 0.8)], fail_inventory=True))
        ep = self.review_episode(self.evidence("alpha", "beta"))
        self.assertEqual(len(self.skill_events(ep)), 2)
        self.assertEqual(self.consolidate_jobs(), [])

    def test_missing_skills_module_keeps_the_review(self):
        self.use(None)
        ep = self.review_episode(self.evidence("alpha", "beta"))
        self.assertEqual(self.skill_events(ep), [])
        self.assertEqual(self.episode_status(ep), "reviewed")

    def test_tool_call_count_queues_one_sweep_until_it_finishes(self):
        self.use(StubSkills())
        self.cfg["consolidate_after_tool_calls"] = 3
        self.review_episode(self.evidence(tool_calls=2))
        self.assertEqual(self.consolidate_jobs(), [])
        self.review_episode(self.evidence(tool_calls=1))
        [job] = self.consolidate_jobs()
        self.assertEqual(job["dedup_key"], review.SWEEP_DEDUP_KEY)
        payload = json.loads(job["payload_json"])
        self.assertEqual((payload["mode"], payload["trigger"], payload["tool_calls"]), ("sweep", "tool_calls", 3))
        self.assertEqual(store.get_meta(self.conn, review.SWEEP_META), iso(self.now))
        # The sweep is not queued twice while it waits.
        self.review_episode(self.evidence(tool_calls=2))
        self.assertEqual([j["state"] for j in self.consolidate_jobs()], ["queued"])

    def test_skill_edits_and_merges_trigger_a_sweep(self):
        self.use(StubSkills())
        self.cfg["consolidate_after_skill_changes"] = 2
        with store.transaction(self.conn):
            for event in ("edit", "merge"):
                self.conn.execute("INSERT INTO skill_events (skill, event, occurred_at) VALUES ('alpha', ?, ?)",
                                  (event, iso(self.now)))
        self.review_episode(self.evidence())
        [job] = self.consolidate_jobs()
        self.assertEqual(json.loads(job["payload_json"])["trigger"], "skill_changes")


if __name__ == "__main__":
    import unittest

    unittest.main()
