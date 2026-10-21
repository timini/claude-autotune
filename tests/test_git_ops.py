import os
import shutil
from pathlib import Path
from unittest import mock

from autotune import git_ops
from autotune.git_ops import GitError
from tests._git_support import GitRepoTestCase, git, snapshot, write


class GitOpsTestCase(GitRepoTestCase):
    def make_worktree(self, cid="cand_1"):
        path = self.worktrees / cid
        git_ops.create_worktree(self.repo, f"autotune/{cid}", self.base, path)
        return path


class ReadTest(GitOpsTestCase):
    def test_head_branch_and_ancestry(self):
        self.assertEqual(git_ops.head(self.repo), self.base)
        self.assertEqual(git_ops.current_branch(self.repo), "main")
        write(self.repo, "CLAUDE.md", "# changed\n")
        git(self.repo, "commit", "-q", "-am", "second")
        second = git_ops.head(self.repo)
        self.assertTrue(git_ops.is_ancestor(self.repo, self.base, second))
        self.assertFalse(git_ops.is_ancestor(self.repo, second, self.base))
        git(self.repo, "checkout", "-q", "--detach", self.base)
        self.assertIsNone(git_ops.current_branch(self.repo))

    def test_is_ancestor_unknown_revision_raises(self):
        with self.assertRaises(GitError) as ctx:
            git_ops.is_ancestor(self.repo, "0" * 40, self.base)
        self.assertEqual(ctx.exception.code, "git_failed")

    def test_dirty_paths_covers_staged_unstaged_untracked_and_renames(self):
        self.assertEqual(git_ops.dirty_paths(self.repo), [])
        write(self.repo, "CLAUDE.md", "# unstaged\n")
        write(self.repo, "staged.txt", "s\n")
        git(self.repo, "add", "staged.txt")
        write(self.repo, ".claude/skills/new/SKILL.md", "untracked\n")
        git(self.repo, "mv", "src/app.py", "src/main.py")
        self.assertEqual(
            git_ops.dirty_paths(self.repo),
            [".claude/skills/new/SKILL.md", "CLAUDE.md", "src/app.py", "src/main.py", "staged.txt"],
        )
        self.assertEqual(
            git_ops.dirty_paths(self.repo, ["CLAUDE.md", ".claude/skills/**"]),
            [".claude/skills/new/SKILL.md", "CLAUDE.md"],
        )
        self.assertEqual(git_ops.dirty_paths(self.repo, [".claude/rules/**"]), [])

    def test_dirty_paths_ignores_gitignored_files(self):
        write(self.repo, "build/out.txt", "x\n")
        self.assertEqual(git_ops.dirty_paths(self.repo), [])

    def test_file_at_and_diff_text(self):
        self.assertEqual(git_ops.file_at(self.repo, self.base, "CLAUDE.md"), b"# Project rules\n")
        self.assertIsNone(git_ops.file_at(self.repo, self.base, "missing.md"))
        self.assertIsNone(git_ops.file_at(self.repo, self.base, "src"))
        write(self.repo, "CLAUDE.md", "# Project rules\n" + "line\n" * 1000)
        git(self.repo, "commit", "-q", "-am", "long")
        full = git_ops.diff_text(self.repo, self.base, "HEAD")
        self.assertIn("+line", full)
        short = git_ops.diff_text(self.repo, self.base, "HEAD", max_bytes=100)
        self.assertLess(len(short.encode()), 160)
        self.assertIn("[diff truncated at 100 bytes]", short)

    def test_missing_git_is_git_unavailable(self):
        empty = self.tmp / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaises(GitError) as ctx:
                git_ops.head(self.repo)
        self.assertEqual(ctx.exception.code, "git_unavailable")

    def test_timeout_is_git_timeout(self):
        with self.assertRaises(GitError) as ctx:
            git_ops.run_git(self.repo, ["status"], timeout=1e-6)
        self.assertEqual(ctx.exception.code, "git_timeout")

    def test_missing_directory_is_git_failed(self):
        with self.assertRaises(GitError) as ctx:
            git_ops.head(self.tmp / "nope")
        self.assertEqual(ctx.exception.code, "git_failed")


class WorktreeTest(GitOpsTestCase):
    def test_hook_fixture_is_live(self):
        git(self.repo, "checkout", "-q", "-b", "other")
        self.assertTrue(self.marker.exists())

    def test_create_worktree_runs_no_hooks_and_leaves_checkout_untouched(self):
        before = snapshot(self.repo)
        wt = self.make_worktree()
        self.assertFalse(self.marker.exists())
        self.assertEqual((wt / "CLAUDE.md").read_text(), "# Project rules\n")
        self.assertEqual(git_ops.head(wt), self.base)
        self.assertEqual(git_ops.current_branch(wt), "autotune/cand_1")
        self.assertTrue(git_ops.branch_exists(self.repo, "autotune/cand_1"))
        self.assertEqual(snapshot(self.repo), before)

    def test_create_worktree_refuses_existing_branch(self):
        self.make_worktree()
        with self.assertRaises(GitError) as ctx:
            git_ops.create_worktree(self.repo, "autotune/cand_1", self.base, self.worktrees / "other")
        self.assertEqual(ctx.exception.code, "git_failed")
        self.assertFalse((self.worktrees / "other").exists())

    def test_create_worktree_refuses_non_autotune_branch(self):
        with self.assertRaises(ValueError):
            git_ops.create_worktree(self.repo, "main2", self.base, self.worktrees / "x")
        with self.assertRaises(ValueError):
            git_ops.create_worktree(self.repo, "autotune/../main", self.base, self.worktrees / "x")
        self.assertFalse(git_ops.branch_exists(self.repo, "main2"))

    def test_remove_worktree_deletes_path_and_branch_and_is_repeatable(self):
        wt = self.make_worktree()
        write(wt, "CLAUDE.md", "edited\n")
        before = snapshot(self.repo)
        git_ops.remove_worktree(self.repo, wt, delete_branch="autotune/cand_1", worktrees_root=self.worktrees)
        self.assertFalse(wt.exists())
        self.assertFalse(git_ops.branch_exists(self.repo, "autotune/cand_1"))
        self.assertEqual(len(git_ops.worktree_paths(self.repo)), 1)
        git_ops.remove_worktree(self.repo, wt, delete_branch="autotune/cand_1", worktrees_root=self.worktrees)
        self.assertFalse(self.marker.exists())
        self.assertEqual(snapshot(self.repo), before)

    def test_remove_worktree_cleans_an_unregistered_leftover_directory(self):
        leftover = self.worktrees / "cand_9"
        write(leftover, "junk.txt", "x\n")
        git_ops.remove_worktree(self.repo, leftover, delete_branch=None, worktrees_root=self.worktrees)
        self.assertFalse(leftover.exists())

    def test_cleanup_of_a_missing_worktree_keeps_the_users_prunable_worktrees(self):
        # The user's own worktree on an unmounted volume: git reports it prunable.
        user_wt = self.tmp / "usb" / "feature"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(user_wt))
        shutil.rmtree(user_wt)
        before = git(self.repo, "worktree", "list", "--porcelain")
        self.assertIn("prunable", before)
        git_ops.remove_worktree(self.repo, self.worktrees / "cand_gone", delete_branch="autotune/cand_gone",
                                worktrees_root=self.worktrees)
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain"), before)

    def test_removed_directory_drops_only_its_own_registration(self):
        wt = self.make_worktree()
        shutil.rmtree(wt)
        git_ops.remove_worktree(self.repo, wt, delete_branch="autotune/cand_1", worktrees_root=self.worktrees)
        self.assertEqual(len(git_ops.worktree_paths(self.repo)), 1)
        self.assertFalse(git_ops.branch_exists(self.repo, "autotune/cand_1"))

    def test_rewritten_git_file_does_not_redirect_staging(self):
        wt = self.make_worktree()
        index = (self.repo / ".git" / "index").read_bytes()
        (wt / ".git").write_text(f"gitdir: {self.repo}/.git\n")
        write(wt, "CLAUDE.md", "edited\n")
        changes = git_ops.changed_files(self.repo, self.base, wt)
        self.assertEqual([c.path for c in changes], ["CLAUDE.md"])
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")
        sha = git_ops.commit_candidate(wt, "m", "cand_1", repo=self.repo)
        self.assertEqual(git_ops.head(self.repo), self.base)
        self.assertEqual(git(self.repo, "rev-parse", "autotune/cand_1").strip(), sha)

    def test_remove_worktree_refuses_unsafe_targets(self):
        wt = self.make_worktree()
        with self.assertRaises(ValueError):
            git_ops.remove_worktree(self.repo, self.repo, delete_branch=None, worktrees_root=self.worktrees)
        with self.assertRaises(ValueError):
            git_ops.remove_worktree(self.repo, self.worktrees, delete_branch=None, worktrees_root=self.worktrees)
        with self.assertRaises(TypeError):
            # The worktrees root is required: no weaker fallback guard exists.
            git_ops.remove_worktree(self.repo, self.repo, delete_branch=None)
        with self.assertRaises(ValueError):
            # A directory named worktrees elsewhere (Claude Code's own) is refused.
            git_ops.remove_worktree(self.repo, self.repo / ".claude" / "worktrees" / "x", delete_branch=None,
                                    worktrees_root=self.worktrees)
        with self.assertRaises(ValueError):
            git_ops.remove_worktree(self.repo, wt, delete_branch="main", worktrees_root=self.worktrees)
        self.assertTrue(wt.exists())
        self.assertTrue((self.repo / "CLAUDE.md").exists())
        self.assertTrue(git_ops.branch_exists(self.repo, "main"))


class ChangesAndCommitTest(GitOpsTestCase):
    def test_changed_files_in_worktree_reports_every_kind_of_edit(self):
        wt = self.make_worktree()
        write(wt, "CLAUDE.md", "# edited\n")
        write(wt, ".claude/rules/new.md", "new\n")
        os.remove(wt / "src/app.py")
        write(wt, ".claude/rules/run.sh", "#!/bin/sh\n", 0o755)
        os.symlink("/etc/hosts", wt / ".claude/rules/link.md")
        write(wt, "build/hidden.md", "ignored but authored\n")
        changes = {c.path: c for c in git_ops.changed_files(self.repo, self.base, wt)}
        self.assertEqual(
            sorted(changes),
            [".claude/rules/link.md", ".claude/rules/new.md", ".claude/rules/run.sh", "CLAUDE.md", "build/hidden.md", "src/app.py"],
        )
        self.assertEqual(changes["CLAUDE.md"].status, "M")
        self.assertEqual(changes["src/app.py"].status, "D")
        self.assertEqual(changes["src/app.py"].new_mode, "000000")
        self.assertEqual(changes[".claude/rules/new.md"].status, "A")
        self.assertEqual(changes[".claude/rules/run.sh"].new_mode, "100755")
        self.assertTrue(changes[".claude/rules/link.md"].is_symlink)
        self.assertFalse(changes["CLAUDE.md"].is_symlink)

    def test_type_change_is_reported(self):
        wt = self.make_worktree()
        os.remove(wt / "CLAUDE.md")
        os.symlink("src/app.py", wt / "CLAUDE.md")
        [change] = git_ops.changed_files(self.repo, self.base, wt)
        self.assertEqual((change.path, change.status, change.is_symlink), ("CLAUDE.md", "T", True))

    def test_commit_candidate_is_attributed_hookless_and_recoverable(self):
        wt = self.make_worktree("cand_abc")
        before = snapshot(self.repo)
        write(wt, "CLAUDE.md", "# better rules\n")
        self.assertEqual(len(git_ops.changed_files(self.repo, self.base, wt)), 1)
        sha = git_ops.commit_candidate(wt, "Tighten verification rule\n\nWhy: tests were claimed.", "cand_abc")
        self.assertFalse(self.marker.exists())
        self.assertEqual(git_ops.head(wt), sha)
        self.assertEqual(git(wt, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip(),
                         "Autotune <autotune@localhost.invalid>|Autotune <autotune@localhost.invalid>")
        body = git(wt, "log", "-1", "--format=%B")
        self.assertTrue(body.rstrip().endswith("Autotune-Candidate: cand_abc"))
        self.assertIn("Why: tests were claimed.", body)
        self.assertEqual(git_ops.find_trailer_commits(self.repo, "cand_abc"), [sha])
        self.assertEqual(git_ops.find_trailer_commits(self.repo, "cand_ab"), [])
        self.assertEqual([c.path for c in git_ops.changed_files(self.repo, self.base, sha)], ["CLAUDE.md"])
        self.assertEqual(git_ops.file_at(self.repo, sha, "CLAUDE.md"), b"# better rules\n")
        self.assertEqual(snapshot(self.repo), before)

    def test_trailer_must_be_in_the_final_paragraph(self):
        write(self.repo, "CLAUDE.md", "x\n")
        git(self.repo, "commit", "-q", "-am", "mentions Autotune-Candidate: cand_x\n\nAutotune-Candidate: cand_x\n\nsigned off")
        write(self.repo, "CLAUDE.md", "y\n")
        git(self.repo, "commit", "-q", "-am", "subject\n\nAutotune-Candidate: cand_x extra")
        self.assertEqual(git_ops.find_trailer_commits(self.repo, "cand_x"), [])

    def test_trailer_survives_branch_reachability_only(self):
        wt = self.make_worktree("cand_q")
        write(wt, "CLAUDE.md", "q\n")
        git_ops.changed_files(self.repo, self.base, wt)
        sha = git_ops.commit_candidate(wt, "q", "cand_q")
        git(self.repo, "branch", "keep", sha)
        git_ops.remove_worktree(self.repo, wt, delete_branch="autotune/cand_q", worktrees_root=self.worktrees)
        self.assertEqual(git_ops.find_trailer_commits(self.repo, "cand_q"), [sha])

    def test_commit_with_nothing_staged_fails(self):
        wt = self.make_worktree()
        with self.assertRaises(GitError) as ctx:
            git_ops.commit_candidate(wt, "empty", "cand_1")
        self.assertEqual(ctx.exception.code, "git_failed")

    def test_candidate_id_cannot_inject_lines(self):
        wt = self.make_worktree()
        write(wt, "CLAUDE.md", "z\n")
        git_ops.changed_files(self.repo, self.base, wt)
        with self.assertRaises(ValueError):
            git_ops.commit_candidate(wt, "m", "cand_1\nSigned-off-by: x")
        with self.assertRaises(ValueError):
            git_ops.find_trailer_commits(self.repo, "cand .*")
