"""Skill inventory, extraction, overlap, merge validation, usage events and the
consolidation rules in case selection and the gate (M6)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from autotune import cases, gate, skills, store
from autotune.util import canonical_json, iso, utc_now
from tests._cycle2_support import case_obj, git

RUNNER = """---
name: test-runner
description: Run the project unit tests and report failing tests. Use when the user asks to run tests.
---
# Test runner

Run `scripts/run-tests.sh` to run the suite.
- Do not report success unless the run printed OK.
- See [notes](notes.md) for flaky tests.
"""

REPORTER = """---
name: test-reporter
description: >
  Run the project unit tests and report failing tests clearly.
  Use when the user asks for a test report.
---
# Test reporter

Summarise failures with file and line.
- Never hide a skipped test count.
- Run `scripts/report.sh` for the summary.
"""

AUDIT = """---
name: release-audit
description: Audit a release before tagging it. Use when the user prepares a release.
---
Check the changelog.
- Only when the release is a major version, ask for a second reviewer.
"""

MERGED = """---
name: test-runner
description: Run the project unit tests and report failing tests clearly. Use when the user asks to run tests or for a test report.
---
# Test runner

Run `scripts/run-tests.sh`, then `scripts/report.sh` for the summary.
- Do not report success unless the run printed OK.
- Never hide a skipped test count.
- See [notes](notes.md) for flaky tests.
"""

SKILL_FILES = {
    ".claude/skills/test-runner/SKILL.md": (RUNNER, False),
    ".claude/skills/test-runner/scripts/run-tests.sh": ("#!/bin/sh\npython3 -m unittest\n", True),
    ".claude/skills/test-runner/notes.md": ("Flaky: none.\n", False),
    ".claude/skills/test-reporter/SKILL.md": (REPORTER, False),
    ".claude/skills/test-reporter/scripts/report.sh": ("#!/bin/sh\necho report\n", True),
    ".claude/skills/release-audit/SKILL.md": (AUDIT, False),
}

RUNNER_DIR = ".claude/skills/test-runner"
REPORTER_DIR = ".claude/skills/test-reporter"
AUDIT_DIR = ".claude/skills/release-audit"


def write_tree(root: Path, files: dict) -> None:
    for rel, (content, executable) in files.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        os.chmod(full, 0o755 if executable else 0o644)


def full_coverage() -> list[dict]:
    return [
        {"source": REPORTER_DIR, "kind": "exception", "item": "Never hide a skipped test count.",
         "destination": "SKILL.md bullets"},
        {"source": REPORTER_DIR, "kind": "support_file", "item": "scripts/report.sh",
         "destination": "scripts/report.sh"},
        {"source": REPORTER_DIR, "kind": "trigger", "item": "Use when the user asks for a test report.",
         "destination": "description"},
    ]


class FrontmatterTests(unittest.TestCase):
    def test_plain_and_folded_values(self):
        meta, body = skills.parse_frontmatter(REPORTER)
        self.assertEqual(meta["name"], "test-reporter")
        self.assertEqual(meta["description"], "Run the project unit tests and report failing tests clearly. "
                                              "Use when the user asks for a test report.")
        self.assertTrue(body.startswith("# Test reporter"))

    def test_literal_block_and_quotes(self):
        meta, _ = skills.parse_frontmatter('---\nname: "q"\ndescription: |\n  line one\n  line two\n---\nbody\n')
        self.assertEqual(meta, {"name": "q", "description": "line one\nline two"})

    def test_no_frontmatter(self):
        meta, body = skills.parse_frontmatter("# just a body\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# just a body\n")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        write_tree(self.root, SKILL_FILES)
        write_tree(self.root, {".claude/skills/nested/group/inner/SKILL.md": ("---\nname: inner\n---\nx\n", False)})

    def test_working_tree_inventory(self):
        inv = skills.by_path(skills.inventory(self.root, {"critical_skills": [AUDIT_DIR]}))
        self.assertEqual(sorted(inv), [".claude/skills/nested/group/inner", AUDIT_DIR, REPORTER_DIR, RUNNER_DIR])
        runner = inv[RUNNER_DIR]
        self.assertEqual(runner.name, "test-runner")
        files = {f.path: f for f in runner.support_files}
        self.assertEqual(sorted(files), ["notes.md", "scripts/run-tests.sh"])
        self.assertTrue(files["scripts/run-tests.sh"].executable)
        self.assertFalse(files["notes.md"].executable)
        self.assertEqual(files["notes.md"].size, len("Flaky: none.\n"))
        links = {(link.kind, link.target): link.resolves for link in runner.links}
        self.assertEqual(links, {("markdown", "notes.md"): True, ("backtick", "scripts/run-tests.sh"): True})
        self.assertTrue(inv[AUDIT_DIR].critical)
        self.assertFalse(inv[AUDIT_DIR].pinned)

    def test_git_inventory_reads_the_commit_not_the_working_tree(self):
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "skills")
        (self.root / RUNNER_DIR / "SKILL.md").write_text("---\nname: changed\n---\n")
        (self.root / REPORTER_DIR / "scripts" / "report.sh").unlink()
        inv = skills.by_path(skills.inventory_at(self.root, "HEAD"))
        self.assertEqual(inv[RUNNER_DIR].name, "test-runner")
        files = {f.path: f for f in inv[REPORTER_DIR].support_files}
        self.assertTrue(files["scripts/report.sh"].executable)
        self.assertEqual(inv[RUNNER_DIR].to_dict()["support_files"],
                         skills.by_path(skills.inventory(self.root))[RUNNER_DIR].to_dict()["support_files"])

    def test_pinned_by_frontmatter_or_config(self):
        write_tree(self.root, {".claude/skills/keep/SKILL.md": ("---\nname: keep\nautotune: pinned\n---\n", False)})
        inv = skills.by_path(skills.inventory(self.root, {"pinned_skills": [".claude/skills/test-*"]}))
        self.assertTrue(inv[".claude/skills/keep"].pinned)
        self.assertTrue(inv[RUNNER_DIR].pinned)
        self.assertFalse(inv[AUDIT_DIR].pinned)

    def test_symlinks_are_listed_by_link_text_and_never_followed(self):
        os.symlink("/etc/hosts", self.root / RUNNER_DIR / "hosts")
        os.symlink("../release-audit", self.root / RUNNER_DIR / "audit-dir")
        inv = skills.by_path(skills.inventory(self.root))
        files = {f.path: f for f in inv[RUNNER_DIR].support_files}
        self.assertEqual(files["hosts"].kind, "symlink")
        self.assertEqual(files["hosts"].sha256, hashlib.sha256(b"/etc/hosts").hexdigest())
        self.assertEqual(files["audit-dir"].kind, "symlink")
        self.assertNotIn("audit-dir/SKILL.md", files)
        self.assertEqual(files["notes.md"].kind, "file")
        self.assertIn("hosts", skills.extract_items(inv[RUNNER_DIR])["support_files"])
        # The same entries come back from the commit.
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "skills")
        at = skills.by_path(skills.inventory_at(self.root, "HEAD"))
        self.assertEqual(at[RUNNER_DIR].to_dict()["support_files"], inv[RUNNER_DIR].to_dict()["support_files"])

    def test_gitlinks_are_listed_from_the_commit_and_the_index(self):
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        sha = "1" * 40
        git(self.root, "update-index", "--add", "--cacheinfo", f"160000,{sha},{RUNNER_DIR}/vendor")
        git(self.root, "commit", "-q", "-m", "skills")
        (self.root / RUNNER_DIR / "vendor").mkdir()
        for inv in (skills.inventory_at(self.root, "HEAD"), skills.inventory(self.root)):
            files = {f.path: f for f in skills.by_path(inv)[RUNNER_DIR].support_files}
            self.assertEqual((files["vendor"].kind, files["vendor"].sha256),
                             ("gitlink", hashlib.sha256(sha.encode()).hexdigest()))

    def test_large_files_are_hashed_in_full(self):
        big = b"x" * (skills.MAX_SKILL_BYTES + 100)
        (self.root / RUNNER_DIR / "model.bin").write_bytes(big)
        want = (len(big), hashlib.sha256(big).hexdigest())
        inv = skills.by_path(skills.inventory(self.root))
        f = {f.path: f for f in inv[RUNNER_DIR].support_files}["model.bin"]
        self.assertEqual((f.size, f.sha256), want)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "skills")
        f = {f.path: f for f in skills.by_path(skills.inventory_at(self.root, "HEAD"))[RUNNER_DIR].support_files}
        self.assertEqual((f["model.bin"].size, f["model.bin"].sha256), want)


class ItemsAndOverlapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write_tree(root, SKILL_FILES)
        self.inv = skills.by_path(skills.inventory(root, {"critical_skills": [AUDIT_DIR]}))

    def test_extract_items(self):
        items = skills.extract_items(self.inv[REPORTER_DIR])
        self.assertEqual(items["triggers"], ["Run the project unit tests and report failing tests clearly.",
                                             "Use when the user asks for a test report."])
        self.assertEqual(items["exceptions"], ["Never hide a skipped test count."])
        self.assertEqual(items["support_files"], ["scripts/report.sh"])
        self.assertEqual(skills.extract_items(self.inv[RUNNER_DIR])["exceptions"],
                         ["Do not report success unless the run printed OK."])
        self.assertEqual(skills.extract_items(self.inv[AUDIT_DIR])["exceptions"],
                         ["Only when the release is a major version, ask for a second reviewer."])

    def test_overlapping_pair_is_found(self):
        pairs = skills.overlaps(list(self.inv.values()), 0.45)
        self.assertEqual([(a, b) for a, b, _, _ in pairs], [(REPORTER_DIR, RUNNER_DIR)])
        self.assertGreaterEqual(pairs[0][2], 0.45)
        self.assertTrue(any("jaccard" in r for r in pairs[0][3]))

    def test_pair_of_protected_skills_is_never_proposed(self):
        both = skills.Skill(name="a", description=self.inv[RUNNER_DIR].description, path="x/a", body="", critical=True)
        other = skills.Skill(name="b", description=self.inv[RUNNER_DIR].description, path="x/b", body="", pinned=True)
        self.assertEqual(skills.overlaps([both, other], 0.1), [])
        free = skills.Skill(name="c", description=self.inv[RUNNER_DIR].description, path="x/c", body="")
        self.assertEqual(len(skills.overlaps([both, free], 0.1)), 1)

    def test_coverage_missing_lists_exceptions_and_files(self):
        reporter = self.inv[REPORTER_DIR]
        self.assertEqual(skills.coverage_missing([reporter], full_coverage()), [])
        partial = [e for e in full_coverage() if e["kind"] != "exception"]
        self.assertEqual(skills.coverage_missing([reporter], partial),
                         [{"source": REPORTER_DIR, "kind": "exception", "item": "Never hide a skipped test count."}])
        blank = [dict(e, destination="") for e in full_coverage()]
        self.assertEqual(len(skills.coverage_missing([reporter], blank)), 2)
        spaced = [dict(full_coverage()[0], item="  never HIDE a skipped\ntest count. ")] + full_coverage()[1:]
        self.assertEqual(skills.coverage_missing([reporter], spaced), [])


class ValidateMergeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base_root = self.root / "base"
        write_tree(self.base_root, SKILL_FILES)
        self.cfg = {"critical_skills": [AUDIT_DIR]}
        self.before = skills.inventory(self.base_root, self.cfg)

    def merged(self, skill_md: str = MERGED, *, executable: bool = True, extra: dict | None = None,
               drop: tuple = (REPORTER_DIR,)) -> list:
        files = {k: v for k, v in SKILL_FILES.items() if not any(k.startswith(d + "/") for d in drop)}
        files[f"{RUNNER_DIR}/SKILL.md"] = (skill_md, False)
        files[f"{RUNNER_DIR}/scripts/report.sh"] = ("#!/bin/sh\necho report\n", executable)
        files.update(extra or {})
        for rel in [k for k, v in files.items() if v is None]:
            del files[rel]
        after_root = self.root / f"after-{len(list(self.root.iterdir()))}"
        write_tree(after_root, files)
        return skills.inventory(after_root, self.cfg)

    def check(self, after, coverage=None, sources=(REPORTER_DIR,)) -> list[str]:
        return [v["code"] for v in skills.validate_merge(self.before, after, coverage or full_coverage(),
                                                          target=RUNNER_DIR, sources=list(sources))]

    def test_correct_merge_has_no_violation(self):
        self.assertEqual(self.check(self.merged()), [])

    def test_dropped_exception(self):
        body = MERGED.replace("- Never hide a skipped test count.\n", "")
        self.assertEqual(self.check(self.merged(body)), ["lost_exception"])

    def test_lost_support_file_and_executable_bit(self):
        files = self.merged(executable=False)
        self.assertEqual(self.check(files), ["executable_bit_changed"])
        after = [s for s in self.merged()]
        for s in after:
            if s.path == RUNNER_DIR:
                s.support_files = [f for f in s.support_files if f.path != "scripts/report.sh"]
        body_ok = self.check(after, coverage=[e for e in full_coverage() if e["kind"] != "support_file"])
        self.assertIn("lost_support_file", body_ok)

    def test_broken_links(self):
        body = MERGED.replace("`scripts/report.sh`", "`../test-reporter/scripts/report.sh`")
        self.assertEqual(self.check(self.merged(body)), ["broken_link"])
        body = MERGED + "Read [the guide](docs/guide.md).\n"
        self.assertEqual(self.check(self.merged(body)), ["broken_link"])
        # The script moved to bin/ but the body still names its old path.
        after = self.merged(extra={f"{RUNNER_DIR}/bin/report.sh": ("#!/bin/sh\necho report\n", True)})
        for s in after:
            if s.path == RUNNER_DIR:
                s.support_files = [f for f in s.support_files if f.path != "scripts/report.sh"]
        self.assertEqual(self.check(after), ["broken_link"])

    def test_removing_a_critical_or_undeclared_skill(self):
        after = self.merged(drop=(REPORTER_DIR, AUDIT_DIR))
        self.assertEqual(self.check(after), ["protected_skill_removed"])
        self.assertEqual(self.check(self.merged(drop=(REPORTER_DIR,)), sources=()), ["protected_skill_removed"])

    def test_vague_destination_does_not_excuse_a_lost_or_changed_file(self):
        vague = [dict(e, destination=f"{REPORTER_DIR}/scripts/report.sh moved under target")
                 if e["kind"] == "support_file" else e for e in full_coverage()]
        # The moved copy is gone: the destination text names the path but no file is there.
        after = self.merged(extra={f"{RUNNER_DIR}/scripts/report.sh": None})
        self.assertIn("lost_support_file", self.check(after, coverage=vague))
        # Rewritten, with or without its executable bit: the moved file must keep its content.
        for exe in (False, True):
            after = self.merged(extra={f"{RUNNER_DIR}/scripts/report.sh": ("#!/bin/sh\necho changed\n", exe)})
            self.assertEqual(self.check(after, coverage=vague), ["lost_support_file"])
            self.assertEqual(self.check(after), ["lost_support_file"])

    def test_target_file_is_not_excused_by_a_non_file_destination(self):
        coverage = full_coverage() + [{"source": REPORTER_DIR, "kind": "exception", "item": "x",
                                       "destination": "SKILL.md next to notes.md"}]
        after = self.merged(MERGED.replace("- See [notes](notes.md) for flaky tests.\n", "- Flaky tests: none.\n"),
                            extra={f"{RUNNER_DIR}/notes.md": None})
        self.assertEqual(self.check(after, coverage=coverage), ["lost_support_file"])

    def test_large_support_file_emptied_is_lost(self):
        big = "y" * (skills.MAX_SKILL_BYTES + 10)
        base = dict(SKILL_FILES)
        base[f"{REPORTER_DIR}/data/model.bin"] = (big, False)
        write_tree(self.base_root, base)
        self.before = skills.inventory(self.base_root, self.cfg)
        coverage = full_coverage() + [{"source": REPORTER_DIR, "kind": "support_file", "item": "data/model.bin",
                                       "destination": "data/model.bin"}]
        self.assertEqual(self.check(self.merged(extra={f"{RUNNER_DIR}/data/model.bin": (big, False)}),
                                    coverage=coverage), [])
        # Emptied, or replaced by other content of the same large size.
        for content in ("", "z" * (skills.MAX_SKILL_BYTES + 10)):
            after = self.merged(extra={f"{RUNNER_DIR}/data/model.bin": (content, False)})
            self.assertEqual(self.check(after, coverage=coverage), ["lost_support_file"])
        self.assertEqual(self.check(self.merged(), coverage=coverage), ["lost_support_file"])

    def test_symlink_in_a_source_must_exist_unchanged_under_the_target(self):
        os.symlink("../../shared/tool.sh", self.base_root / REPORTER_DIR / "tool.sh")
        self.before = skills.inventory(self.base_root, self.cfg)
        reporter = skills.by_path(self.before)[REPORTER_DIR]
        self.assertIn({"source": REPORTER_DIR, "kind": "support_file", "item": "tool.sh"},
                      skills.coverage_missing([reporter], full_coverage()))
        coverage = full_coverage() + [{"source": REPORTER_DIR, "kind": "support_file", "item": "tool.sh",
                                       "destination": "tool.sh"}]
        after = self.merged()
        self.assertEqual(self.check(after, coverage=coverage), ["lost_support_file"])
        after_root = self.root / f"after-{len(list(self.root.iterdir()))}"
        write_tree(after_root, {k: v for k, v in SKILL_FILES.items() if not k.startswith(REPORTER_DIR + "/")})
        write_tree(after_root, {f"{RUNNER_DIR}/SKILL.md": (MERGED, False),
                                f"{RUNNER_DIR}/scripts/report.sh": ("#!/bin/sh\necho report\n", True)})
        os.symlink("../../shared/tool.sh", after_root / RUNNER_DIR / "tool.sh")
        self.assertEqual(self.check(skills.inventory(after_root, self.cfg), coverage=coverage), [])

    def test_lost_trigger(self):
        body = MERGED.replace(
            "description: Run the project unit tests and report failing tests clearly. Use when the user asks to run "
            "tests or for a test report.", "description: Lint Python files.")
        got = skills.validate_merge(self.before, self.merged(body), full_coverage(), target=RUNNER_DIR,
                                    sources=[REPORTER_DIR])
        self.assertEqual(sorted((v["code"], v["path"]) for v in got),
                         [("trigger_lost", REPORTER_DIR), ("trigger_lost", RUNNER_DIR)])

    def test_trigger_keep_fraction_boundary(self):
        trigger = "Plan and check database schema migrations for release builds."
        self.assertEqual(len(set(skills._content_words(trigger))), 7)
        for kept, want in ((["plan", "check"], False), (["plan", "check", "database"], False),
                           (["plan", "check", "database", "schema"], True)):
            text = " ".join(kept)
            self.assertIs(skills._trigger_kept(trigger, text, set(kept)), want, kept)
        # Through validate_merge: "Run tests." keeps half of the target's
        # "Use when the user asks to run tests." but under half of each source trigger.
        body = MERGED.replace(
            "description: Run the project unit tests and report failing tests clearly. Use when the user asks to run "
            "tests or for a test report.", "description: Run tests.")
        self.assertEqual(self.check(self.merged(body)), ["trigger_lost"])
        body = MERGED.replace(
            "description: Run the project unit tests and report failing tests clearly. Use when the user asks to run "
            "tests or for a test report.", "description: Run project unit tests and report failures. Use when the "
                                           "user asks to run tests.")
        self.assertEqual(self.check(self.merged(body)), [])

    def test_target_triggers_are_kept_for_a_merge_and_an_update(self):
        # Keeps the source's trigger, drops both of the target's.
        body = MERGED.replace(
            "description: Run the project unit tests and report failing tests clearly. Use when the user asks to run "
            "tests or for a test report.", "description: Write a test report.")
        got = skills.validate_merge(self.before, self.merged(body), full_coverage(), target=RUNNER_DIR,
                                    sources=[REPORTER_DIR])
        self.assertEqual([(v["code"], v["path"]) for v in got], [("trigger_lost", RUNNER_DIR)])
        # An update has no sources; the target's own triggers still count.
        after = self.merged(body, drop=())
        got = skills.validate_merge(self.before, after, [], target=RUNNER_DIR, sources=[])
        self.assertEqual([(v["code"], v["path"]) for v in got], [("trigger_lost", RUNNER_DIR)])


def insert_episode(conn, episode_id: str, evidence: list[tuple[str, str, dict]], now) -> None:
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO episodes (episode_id, session_id, turn_id, lifecycle_id, lifecycle_source, origin, "
            "completed_at, captured_at, end_reason, repository_json, loaded_harness_json, coverage_json) "
            "VALUES (?, ?, ?, ?, 'test', 'foreground', ?, ?, 'answer', '{}', '{}', '{}')",
            (episode_id, f"s-{episode_id}", f"t-{episode_id}", f"l-{episode_id}", iso(now), iso(now)),
        )
        for i, (kind, content, prov) in enumerate(evidence, start=1):
            conn.execute(
                "INSERT INTO evidence (episode_id, evidence_id, position, kind, role, content, content_sha256, "
                "provenance_json, retain_until) VALUES (?, ?, ?, ?, 'system', ?, 'x', ?, ?)",
                (episode_id, f"e{i:04d}", i, kind, content, canonical_json(prov), iso(now)),
            )


class UsageEventTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "autotune.db")
        self.addCleanup(self.conn.close)
        self.now = utc_now()

    def events(self, event=None) -> list[tuple]:
        sql = "SELECT event, skill, path, candidate_id, detail_json FROM skill_events"
        rows = self.conn.execute(sql + (" WHERE event = ?" if event else "") + " ORDER BY id",
                                 (event,) if event else ()).fetchall()
        return [tuple(r) for r in rows]

    def test_load_invocation_and_outcome_are_separate_rows(self):
        insert_episode(self.conn, "ep1", [
            ("skill_load", canonical_json({"skill": "test-runner", "source": "projectSettings",
                                           "path": "/r/.claude/skills/test-runner/SKILL.md"}), {"source": "skill.load"}),
            ("skill_load", canonical_json({"skill": "release-audit", "source": None, "path": None}), {}),
            ("tool_call", '{"skill": "test-runner"}', {"source": "tool.call", "tool": "Skill"}),
            ("tool_call", "python3 -m unittest", {"source": "tool.call", "tool": "Bash"}),
        ], self.now)
        with store.transaction(self.conn):
            self.assertEqual(skills.record_episode(self.conn, "ep1", self.now), 3)
        self.assertEqual([e[:2] for e in self.events()],
                         [("load", "test-runner"), ("load", "release-audit"), ("invocation", "test-runner")])
        # No review yet: no outcome row is inferred from a load or a call.
        self.assertEqual(self.events("outcome"), [])
        review = {"dimensions": {"outcome": {"score": 3}}}
        with store.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO reviews (review_id, episode_id, task_stage, review_json, recommendation, created_at) "
                "VALUES ('rev1', 'ep1', 'claimed_complete', ?, 'no_change', ?)", (json.dumps(review), iso(self.now)))
            skills.record_episode(self.conn, "ep1", self.now)
        outcomes = self.events("outcome")
        self.assertEqual([o[1] for o in outcomes], ["test-runner", "release-audit"])
        self.assertEqual(json.loads(outcomes[0][4]), {"outcome_score": 3, "recommendation": "no_change",
                                                      "review_id": "rev1"})
        self.assertEqual(len(self.events("load")), 2, "a rerun replaces, it does not double count")

    def test_record_merge_links_sources_to_the_candidate(self):
        now = iso(self.now)
        info = {"consolidation": {"target": RUNNER_DIR, "sources": [REPORTER_DIR], "skills": [RUNNER_DIR, REPORTER_DIR]}}
        with store.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO candidates (candidate_id, hypothesis, effect_metric, allowed_paths_json, change_kind, "
                "base_commit, status, validation_json, created_at, updated_at) "
                "VALUES ('cand1', 'h', 'm', '[]', 'consolidation', 'b', 'applied', ?, ?, ?)",
                (canonical_json(info), now, now))
        row = dict(self.conn.execute("SELECT * FROM candidates").fetchone())
        with store.transaction(self.conn):
            self.assertEqual(skills.record_merge(self.conn, row, self.now), 1)
            self.assertEqual(skills.record_merge(self.conn, row, self.now), 0)
        merges = self.events("merge")
        self.assertEqual([(m[0], m[2], m[3]) for m in merges], [("merge", REPORTER_DIR, "cand1")])
        self.assertEqual(skills.record_merge(self.conn, {**row, "change_kind": "skill"}, self.now), 0)


class ConsolidationSelectionTests(unittest.TestCase):
    def make(self, cid, kind, harness):
        case, errors = cases.validate_case(case_obj(cid, kind, [{"id": "g", "type": "answer_contains", "text": "x"}],
                                                    harness=harness))
        self.assertEqual(errors, [])
        return case

    def test_cases_touching_either_skill_plus_a_negative_trigger(self):
        loaded = [
            self.make("near-runner", "protected", [f"{RUNNER_DIR}/SKILL.md"]),
            self.make("near-all", "target", [".claude/skills/**"]),
            self.make("neg", "negative_trigger", [".claude/skills/**"]),
            self.make("elsewhere", "protected", ["CLAUDE.md"]),
            self.make("gen", "general", ["CLAUDE.md"]),
        ]
        row = {"change_kind": "consolidation", "allowed_paths_json": canonical_json(
            [RUNNER_DIR + "/**", REPORTER_DIR + "/**"]), "required_cases_json": "[]"}
        self.assertEqual([c.id for c in cases.select_required(loaded, row)], ["near-all", "near-runner", "neg"])
        stored = {**row, "allowed_paths_json": "[]", "validation_json": canonical_json(
            {"consolidation": {"skills": [RUNNER_DIR, REPORTER_DIR]}})}
        self.assertEqual([c.id for c in cases.select_required(loaded, stored)], ["near-all", "near-runner", "neg"])

    def test_no_negative_trigger_case_selects_nothing(self):
        loaded = [self.make("near-runner", "protected", [f"{RUNNER_DIR}/SKILL.md"]),
                  self.make("neg-elsewhere", "negative_trigger", ["CLAUDE.md"])]
        row = {"change_kind": "consolidation", "allowed_paths_json": canonical_json([RUNNER_DIR + "/**"])}
        self.assertEqual(cases.select_required(loaded, row), [])
        inputs = {"candidate": {"change_kind": "consolidation"}, "cases": [], "runs": []}
        self.assertEqual(gate.decide(inputs)["reason_codes"], ["no_required_cases"])


class ConsolidationGateTests(unittest.TestCase):
    """The consolidation target rule on recorded runs (tests/test_gate.py helpers)."""

    def build(self, *, tokens=(1000, 800), outcomes=None):
        from tests import test_gate as tg

        suite = [tg.case("t1", "target"), tg.case("p1", "protected"), tg.case("n1", "negative_trigger")]
        plan = {c["id"]: {"baseline": ["pass"], "candidate": ["pass"]} for c in suite}
        plan.update(outcomes or {})
        inputs = tg.build(cases=suite, outcomes=plan,
                          candidate={"candidate_id": "c", "base_commit": tg.BASE, "candidate_commit": tg.CAND,
                                     "change_kind": "consolidation"})
        if tokens is not None:
            inputs["consolidation"] = {"baseline_tokens": tokens[0], "candidate_tokens": tokens[1],
                                       "skills": [RUNNER_DIR, REPORTER_DIR]}
        return inputs

    def test_equivalent_outcomes_and_ten_percent_fewer_tokens_accept(self):
        result = gate.decide(self.build(tokens=(1000, 900)))
        self.assertEqual(result["decision"], "accept", result["details"])

    def test_too_few_tokens_saved_rejects(self):
        result = gate.decide(self.build(tokens=(1000, 901)))
        self.assertEqual(result["decision"], "reject")
        self.assertIn("target_not_met", result["reason_codes"])

    def test_candidate_only_success_is_not_equivalent(self):
        result = gate.decide(self.build(outcomes={"t1": {"baseline": ["fail"], "candidate": ["pass"]}}))
        self.assertEqual(result["decision"], "inconclusive")
        self.assertEqual(result["reason_codes"], ["outcomes_not_equivalent"])

    def test_baseline_success_lost_rejects(self):
        result = gate.decide(self.build(outcomes={"n1": {"baseline": ["pass"], "candidate": ["fail"]}}))
        self.assertEqual(result["decision"], "reject")
        self.assertIn("regression_observed", result["reason_codes"])

    def test_missing_metric_or_negative_trigger_is_inconclusive(self):
        result = gate.decide(self.build(tokens=None))
        self.assertEqual((result["decision"], result["reason_codes"]), ("inconclusive", ["consolidation_metric_unknown"]))
        inputs = self.build()
        inputs["cases"] = [c for c in inputs["cases"] if c["kind"] != "negative_trigger"]
        inputs["runs"] = [r for r in inputs["runs"] if r["case_id"] != "n1"]
        result = gate.decide(inputs)
        self.assertEqual((result["decision"], result["reason_codes"]), ("inconclusive", ["no_required_cases"]))


if __name__ == "__main__":
    unittest.main()
