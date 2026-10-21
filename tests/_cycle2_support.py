"""Fixtures for cycle 2 tests (evaluate, apply, revert): a real temporary Git
repo with committed cases, candidates committed on real `autotune/<id>`
branches, the scripted fake CLI, and direct handler runs through the real
queue and worker outcome recording."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotune import config, git_ops, paths, queue, store, worker
from autotune.util import canonical_json, iso, new_id, utc_now

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
FAMILY = "verification-reporting"
VERIFY_RULE = "Run the focused test before reporting."
MARKER = "Report failing tests as failing."
BASE_CLAUDE_MD = f"# Project rules\n\n{VERIFY_RULE}\n"
USEFUL_CLAUDE_MD = BASE_CLAUDE_MD + f"{MARKER}\n"
NO_VERIFY_CLAUDE_MD = f"# Project rules\n\n{MARKER}\n"

_GIT_ID = ["-c", "user.name=Autotune Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo, *args) -> str:
    out = subprocess.run(["git", *_GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True,
                         env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return out.stdout.strip()


def snapshot(repo: Path) -> dict:
    """Every file outside .git (mode and hash), the index, status, HEAD and branch."""
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
        "branch": git(repo, "symbolic-ref", "--short", "HEAD") if _on_branch(repo) else None,
    }


def _on_branch(repo: Path) -> bool:
    return subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo, capture_output=True,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).returncode == 0


def case_obj(case_id: str, kind: str, graders: list[dict], *, critical: bool = False, family: str = FAMILY,
             harness=("CLAUDE.md",), trials: int = 1, judge=None, request: str = "Run the test and report.") -> dict:
    return {
        "id": case_id,
        "task_family": family,
        "kind": kind,
        "critical": critical,
        "description": f"{kind} case {case_id}",
        "request": request,
        "fixture": {"files": {"calc.py": "def add(a, b):\n    return a - b\n"}, "from_commit": None},
        "harness_paths": list(harness),
        "allowed_tools": ["Read", "Bash"],
        "allowed_bash": ["python3 -m unittest*"],
        "network": "not_required",
        "trials_per_arm": trials,
        "timeout_seconds": 60,
        "graders": graders,
        "judge": judge,
    }


def standard_cases() -> list[dict]:
    """Two target cases that need the marker rule and one protected critical
    case that needs the verification rule (the agent must run Bash)."""
    reports = [{"id": "reports-failing", "type": "answer_contains", "text": "FAILING", "critical": False}]
    return [
        case_obj("target-alpha", "target", reports),
        case_obj("target-beta", "target", reports),
        case_obj("protected-verify", "protected", [{"id": "ran-tests", "type": "tool_used", "tool": "Bash"}],
                 critical=True),
    ]


def replay_responses(*, candidate_model: str = "claude-fake-1") -> list[dict]:
    """Fake replay answers that depend only on the workspace CLAUDE.md."""
    bash = [{"name": "Bash", "input": {"command": "python3 -m unittest"}, "result": "FAILED (failures=1)"}]
    return [
        {"role": "replay", "when_file_contains": {"path": "CLAUDE.md", "text": MARKER},
         "when_file_not_contains": {"path": "CLAUDE.md", "text": VERIFY_RULE},
         "result": "The test is FAILING.", "model": candidate_model},
        {"role": "replay", "when_file_contains": {"path": "CLAUDE.md", "text": MARKER},
         "result": "Ran the test. It is FAILING.", "tool_uses": bash, "model": candidate_model},
        {"role": "replay", "when_file_contains": {"path": "CLAUDE.md", "text": VERIFY_RULE},
         "result": "Ran the test. All tests pass.", "tool_uses": bash},
        {"role": "replay", "result": "All tests pass."},
    ]


class Cycle2Case(unittest.TestCase):
    """An enrolled repo at self.repo with committed cases, AUTOTUNE_HOME in a
    temp dir and the fake CLI configured (no real model can be reached)."""

    repo_config: dict = {"enabled": True}
    cases: list[dict] | None = None
    check_checkout_unchanged = True

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
        for key in ("AUTOTUNE_ORIGIN", "ANTHROPIC_API_KEY", "AUTOTUNE_FAKE_AUTH", "AUTOTUNE_TEST_FAULT",
                    "AUTOTUNE_TEST_FAULTS_ENABLED"):
            os.environ.pop(key, None)
        self.script(*replay_responses())

        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        files = {
            ".autotune.json": json.dumps(self.repo_config),
            "CLAUDE.md": BASE_CLAUDE_MD,
            "src/app.py": "print('app')\n",
        }
        for case in (standard_cases() if self.cases is None else self.cases):
            files[f".autotune/cases/{case['id']}.json"] = json.dumps(case, indent=1)
        for rel, content in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.base = git(self.repo, "rev-parse", "HEAD")
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.data_dir = paths.project_data_dir(common)
        self.db_path = paths.db_path(self.data_dir)
        self.conn = store.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.cfg = config.load_config(self.repo)

    # fake CLI

    def script(self, *responses) -> None:
        self.fake_script.write_text(json.dumps({"responses": list(responses)}))
        Path(str(self.fake_script) + ".idx").unlink(missing_ok=True)

    def fake_calls(self) -> list[dict]:
        if not self.fake_log.exists():
            return []
        lines = [json.loads(line) for line in self.fake_log.read_text().splitlines() if line.strip()]
        return [entry for entry in lines if "-p" in entry["argv"]]

    # candidates

    def make_candidate(self, files: dict, *, status: str = "validating", base: str | None = None,
                       evaluated: bool = False, **cols) -> dict:
        """Commit `files` on a real autotune/<id> branch in a worktree under the
        data dir, as propose would, and insert the candidate row."""
        base = base or git(self.repo, "rev-parse", "HEAD")
        cid = new_id("cand")
        branch = f"autotune/{cid}"
        wt = self.data_dir / "worktrees" / cid
        git_ops.create_worktree(self.repo, branch, base, str(wt))
        for rel, content in files.items():
            target = wt / rel
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        changes = git_ops.changed_files(self.repo, base, Path(wt))
        sha = git_ops.commit_candidate(str(wt), f"Autotune candidate {cid}\n\nTest change.", cid, repo=self.repo)
        now = utc_now()
        row = {
            "candidate_id": cid,
            "source_reviews_json": "[]",
            "hypothesis": "Report failing tests (claims-tests-pass-when-failing)",
            "effect_metric": "target failure signature no longer observed",
            "allowed_paths_json": canonical_json(["CLAUDE.md"]),
            "change_kind": "instruction",
            "branch": branch,
            "base_commit": base,
            "candidate_commit": sha,
            "required_cases_json": "[]",
            "status": status,
            "failure_signature": "claims-tests-pass-when-failing",
            "created_at": iso(now),
            "updated_at": iso(now),
            "worktree_path": str(wt),
            "validation_json": canonical_json({"ok": True, "needs_scope_change": False, "violations": [],
                                               "files": sorted(c.path for c in changes)}),
            "target_branch": "main",
            "task_family": FAMILY,
        }
        if evaluated:
            from autotune import evaluate

            ctx = self.context()
            row["evaluator_commit"] = canonical_json(evaluate.evaluator_identity(ctx, self.repo, base))
            row["policy_digest"] = evaluate.policy_digest(self.repo)
            row["required_cases_json"] = canonical_json([c["id"] for c in standard_cases()])
        row.update(cols)
        names = list(row)
        with store.transaction(self.conn):
            self.conn.execute(f"INSERT INTO candidates ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                              [row[n] for n in names])
        return self.candidate(cid)

    def candidate(self, cid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (cid,)).fetchone())

    def runs(self, cid: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM evaluation_runs WHERE candidate_id = ? ORDER BY case_id, trial, arm, attempt", (cid,))]

    def jobs(self, job_type: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM jobs WHERE job_type = ? ORDER BY id", (job_type,))]

    # running handlers

    def context(self, *, clock=None, cfg: dict | None = None, job_id: int = 0, attempt: int = 1):
        clock = clock or utc_now
        return worker.Context(
            db_path=self.db_path, data_dir=self.data_dir, project_dir=str(self.repo),
            config=copy.deepcopy(cfg if cfg is not None else self.cfg), worker_id="w-test", clock=clock,
            logger=worker.JsonLog(None, clock), job_id=job_id, attempt=attempt,
        )

    def run_job(self, job_type: str, candidate_id: str, *, payload: dict | None = None, dedup: str | None = None,
                clock=None, cfg: dict | None = None, ctx_hook=None):
        """Enqueue (or reuse) a job, lease it, run its registered handler and
        record the outcome the way the worker does. Returns (HandlerResult, job row)."""
        clock = clock or utc_now
        now = clock()
        job_id, _ = queue.enqueue(self.conn, job_type, dedup or f"{job_type}:{candidate_id}",
                                  payload or {"candidate_id": candidate_id}, now, candidate_id=candidate_id)
        with store.transaction(self.conn):
            self.conn.execute("UPDATE jobs SET state = 'queued', next_eligible_at = ? WHERE id = ?", (iso(now), job_id))
        job = queue.lease(self.conn, "w-test", now, 300, job_types=[job_type])
        self.assertIsNotNone(job, "job was not leasable")
        self.assertEqual(job["id"], job_id)
        ctx = self.context(clock=clock, cfg=cfg, job_id=job_id, attempt=job["attempts"])
        if ctx_hook is not None:
            ctx_hook(ctx)
        res = worker.HANDLERS[job_type](ctx, job)
        worker._record_outcome(self.conn, job, "w-test", clock(), res, 5)
        return res, dict(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
