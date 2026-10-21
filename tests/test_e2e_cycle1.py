"""Cycle 1 end to end: a captured turn is reviewed and a candidate is authored.

Real ingress, real worker loop with the default handlers, real adapter, the fake
CLI (tests/fake_claude.py) and a real temporary Git repo. Every test checks that
the user's checkout is byte-identical at the end.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotune import claude_cli, cli, git_ops, ingress, paths, propose, queue, review, store, worker
from tests._runtime_support import envelope
from tests.test_review import make_review

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
SIG = "claims-tests-pass-when-failing"
DIMENSIONS = ("outcome", "instruction_adherence", "verification", "efficiency", "communication")
SUMMARY = {
    "summary": "Added a rule to report failing tests.",
    "changed_behavior": "The agent reports a failing focused test instead of claiming success.",
    "potential_regressions": ["Longer final answers."],
    "files_changed": ["CLAUDE.md"],
}
_GIT_ID = ["-c", "user.name=Autotune Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo, *args) -> str:
    out = subprocess.run(["git", *_GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True,
                         env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return out.stdout.strip()


def snapshot(repo: Path) -> dict:
    files = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            full = Path(dirpath) / name
            data = os.readlink(full).encode() if full.is_symlink() else full.read_bytes()
            files[full.relative_to(repo).as_posix()] = (full.lstat().st_mode, hashlib.sha256(data).hexdigest())
    return {
        "files": files,
        "index": hashlib.sha256((repo / ".git" / "index").read_bytes()).hexdigest(),
        "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "symbolic-ref", "--short", "HEAD"),
    }


class CycleOneCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.fake_log = self.root / "fake.log"
        self.fake_script = self.root / "script.json"
        patcher = mock.patch.dict(os.environ, {
            "AUTOTUNE_HOME": str(self.root / "home"),
            "AUTOTUNE_NO_WORKER": "1",
            "AUTOTUNE_CLAUDE": str(FAKE_CLAUDE),
            "AUTOTUNE_FAKE_LOG": str(self.fake_log),
            "AUTOTUNE_FAKE_SCRIPT": str(self.fake_script),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("AUTOTUNE_ORIGIN", "ANTHROPIC_API_KEY", "AUTOTUNE_FAKE_AUTH"):
            os.environ.pop(key, None)

        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        for rel, content in {
            ".autotune.json": json.dumps({"enabled": True}),
            "CLAUDE.md": "# Project rules\n",
            "src/app.py": "print('app')\n",
        }.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.data_dir = paths.project_data_dir(common)
        self.before = snapshot(self.repo)
        self.addCleanup(lambda: self.assertEqual(snapshot(self.repo), self.before))

    # helpers

    def script(self, *responses) -> None:
        self.fake_script.write_text(json.dumps({"responses": list(responses)}))
        Path(str(self.fake_script) + ".idx").unlink(missing_ok=True)

    def reviewer(self, **kw) -> dict:
        kw.setdefault("recommendation", "propose_candidate")
        return {"role": "worker", "structured_output": make_review("$echo_episode", **kw)}

    @staticmethod
    def author(edits) -> dict:
        return {"role": "author", "structured_output": SUMMARY, "result": "done", "edits": edits}

    def ingest_turn(self) -> dict:
        env = envelope("turn_complete", self.repo, tool_calls=[{
            "tool": "Bash", "tool_use_id": "tu1", "input_summary": "python -m unittest",
            "is_error": True, "result_excerpt": "FAILED (failures=1)"}])
        out = io.StringIO()
        code = ingress.main(io.BytesIO(json.dumps(env).encode()), out, dict(os.environ))
        ack = json.loads(out.getvalue())
        self.assertEqual(code, 0, ack)
        self.assertTrue(ack["ok"], ack)
        return ack

    def run_worker(self) -> None:
        # The cycle 1 flow ends at a validating candidate; evaluation and
        # application are covered by tests/test_e2e_cycle2.py.
        handlers = {t: worker.HANDLERS[t] for t in ("review", "propose")}
        rc = worker.run_worker(str(self.repo), idle_seconds=0.3, poll_seconds=0.05, handlers=handlers)
        self.assertEqual(rc, 0)

    def cli(self, *args) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([*args, "--project-dir", str(self.repo)])
        return code, out.getvalue()

    def db(self):
        conn = store.connect(paths.db_path(self.data_dir))
        self.addCleanup(conn.close)
        return conn

    def jobs(self, conn, job_type) -> list:
        return conn.execute("SELECT * FROM jobs WHERE job_type = ? ORDER BY id", (job_type,)).fetchall()

    def print_calls(self) -> int:
        if not self.fake_log.exists():
            return 0
        lines = [json.loads(line) for line in self.fake_log.read_text().splitlines() if line.strip()]
        return sum(1 for entry in lines if "-p" in entry["argv"])

    def assert_no_candidate_git_state(self) -> None:
        self.assertEqual(git(self.repo, "branch", "--list", "autotune/*"), "")
        self.assertEqual(len(git(self.repo, "worktree", "list", "--porcelain").split("\n\n")), 1)
        wt_root = self.data_dir / "worktrees"
        self.assertEqual(list(wt_root.iterdir()) if wt_root.exists() else [], [])

    # tests

    def test_turn_is_reviewed_and_a_candidate_is_authored(self):
        self.script(self.reviewer(), self.author({"CLAUDE.md": "# Project rules\n\nReport failing tests.\n"}))
        ack = self.ingest_turn()
        self.run_worker()
        conn = self.db()

        rows = conn.execute("SELECT * FROM reviews").fetchall()
        self.assertEqual(len(rows), 1)
        rev = rows[0]
        self.assertEqual(rev["status"], "valid")
        self.assertEqual(rev["episode_id"], ack["episode_id"])
        self.assertEqual(rev["reviewer_model"], "claude-fake-1")
        self.assertEqual(rev["failure_signature"], SIG)
        dims = json.loads(rev["review_json"])["dimensions"]
        self.assertEqual(sorted(dims), sorted(DIMENSIONS))
        self.assertEqual([dims[d]["score"] for d in DIMENSIONS], [1, 2, 0, None, 1])
        self.assertEqual(self.jobs(conn, "review")[0]["state"], "succeeded")

        proposes = self.jobs(conn, "propose")
        self.assertEqual(len(proposes), 1)
        self.assertEqual(proposes[0]["state"], "succeeded")
        self.assertEqual(json.loads(proposes[0]["payload_json"])["review_id"], rev["review_id"])

        cands = conn.execute("SELECT * FROM candidates").fetchall()
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        cid = cand["candidate_id"]
        self.assertEqual(cand["status"], "validating")
        self.assertEqual(cand["branch"], f"autotune/{cid}")
        self.assertEqual(cand["base_commit"], self.before["head"])
        wt = Path(cand["worktree_path"])
        self.assertTrue(wt.is_dir())
        self.assertEqual(wt.parent, self.data_dir / "worktrees")
        self.assertIn(str(wt), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual((wt / "CLAUDE.md").read_text(), "# Project rules\n\nReport failing tests.\n")
        self.assertEqual(git_ops.find_trailer_commits(self.repo, cid), [cand["candidate_commit"]])
        self.assertEqual(git(self.repo, "rev-parse", cand["branch"]), cand["candidate_commit"])
        self.assertEqual(git(self.repo, "diff", "--name-only", cand["base_commit"], cand["candidate_commit"]),
                         "CLAUDE.md")

        evals = self.jobs(conn, "evaluate")
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0]["state"], "queued")
        self.assertEqual(evals[0]["dedup_key"], f"evaluate:{cid}")
        self.assertEqual(evals[0]["candidate_id"], cid)

        roles = [r["role"] for r in conn.execute("SELECT role FROM model_calls ORDER BY started_at")]
        self.assertEqual(roles, ["review", "author"])
        self.assertEqual(self.print_calls(), 2)

        code, text = self.cli("review")
        self.assertEqual(code, 0)
        self.assertIn(ack["episode_id"], text)
        self.assertIn("claude-fake-1", text)
        code, text = self.cli("candidate", cid)
        self.assertEqual(code, 0)
        self.assertIn("validating", text)
        self.assertIn("Report failing tests.", text)

    def test_fictitious_evidence_id_fails_after_one_repair(self):
        self.script(self.reviewer(ids=("e0001", "e9999")))
        self.ingest_turn()
        self.run_worker()
        conn = self.db()

        job = self.jobs(conn, "review")[0]
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["last_error"], "invalid_evidence_reference")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 2)
        self.assertEqual(self.print_calls(), 2)
        statuses = [r["status"] for r in conn.execute("SELECT status FROM reviews")]
        self.assertEqual(statuses, ["rejected"])
        self.assertEqual(self.jobs(conn, "propose"), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assert_no_candidate_git_state()

    def test_source_edit_outside_scope_leaves_no_branch(self):
        # Only outside_scope violations: the contract records this as a policy
        # question (needs_scope_change), not a malformed patch.
        self.script(self.reviewer(), self.author({"src/app.py": "import os\nos.system('true')\n"}))
        self.ingest_turn()
        self.run_worker()
        conn = self.db()

        cand = conn.execute("SELECT * FROM candidates").fetchone()
        self.assertEqual(cand["status"], "needs_scope_change")
        self.assertEqual(cand["reason"], "needs_scope_change")
        self.assertIsNone(cand["candidate_commit"])
        violations = json.loads(cand["validation_json"])["violations"]
        self.assertIn(("outside_scope", "src/app.py"), [(v["code"], v["path"]) for v in violations])
        self.assertEqual(self.jobs(conn, "evaluate"), [])
        self.assertIn("src/app.py", propose.candidate_diff_path(self.data_dir, cand["candidate_id"]).read_text())
        self.assert_no_candidate_git_state()

    def test_source_and_evaluator_edit_is_rejected_as_scope_violation(self):
        self.script(self.reviewer(), self.author({"src/app.py": "bad\n",
                                                  ".autotune/cases/x.json": "{\"id\": \"x\"}"}))
        self.ingest_turn()
        self.run_worker()
        conn = self.db()

        cand = conn.execute("SELECT * FROM candidates").fetchone()
        self.assertEqual(cand["status"], "rejected")
        self.assertEqual(cand["reason"], "scope_violation")
        codes = {(v["code"], v["path"]) for v in json.loads(cand["validation_json"])["violations"]}
        self.assertIn(("outside_scope", "src/app.py"), codes)
        self.assertIn(("protected_path", ".autotune/cases/x.json"), codes)
        self.assertEqual(self.jobs(conn, "evaluate"), [])
        self.assert_no_candidate_git_state()


class ErrorCodeCoverageTest(unittest.TestCase):
    def test_every_emitted_code_is_classified(self):
        emitted = set(claude_cli.ERROR_CODES) | {
            "git_unavailable", "git_failed", "git_timeout",
            "evidence_expired", "invalid_evidence_reference", "invalid_output", "episode_mismatch",
            "candidate_slot_busy", "checkout_dirty", "no_change", "scope_violation", "needs_scope_change",
            "commit_failed", "handler_unavailable", "invalid_handler_result", "disk_cap",
        }
        self.assertEqual(sorted(emitted - set(queue._CODE_CLASSES)), [])

    def test_real_handlers_are_registered(self):
        self.assertIs(worker.HANDLERS["review"], review.review_handler)
        self.assertIs(worker.HANDLERS["propose"], propose.propose_handler)
        self.assertIn("evaluate", worker.HANDLERS)
        self.assertEqual(worker.HANDLER_IMPORT_ERRORS, [])


if __name__ == "__main__":
    unittest.main()
