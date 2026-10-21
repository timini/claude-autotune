# Operations

How to pause, stop, clean up, undo and remove Autotune, and how to recover when it stops itself. Every command
below can be typed in a session as `/autotune <command>`, or run from the plugin directory as
`python3 -m autotune <command> --project-dir /path/to/repo`. `autotune status` shows the current state, including
why work is waiting.

## Where things live

| What | Where |
|---|---|
| Enrollment and policy | `.autotune.json` in the repository (committed by you; `autotune enable` writes it only when it is missing, and nothing else in Autotune edits it) |
| Database, logs, locks | `~/.claude/autotune/<project-id>/` (`AUTOTUNE_HOME` replaces `~/.claude/autotune`); the database is `autotune.sqlite3`, the log `logs/autotune.log` |
| Candidate and revert worktrees | `<data dir>/worktrees/<candidate-id>` and `<data dir>/worktrees/revert-<candidate-id>` |
| Candidate and revert branches | `autotune/<candidate-id>`, `autotune/revert-<candidate-id>` in your repository |
| Replay workspaces | `autotune-replay/` under the system temporary directory, removed after each run |
| Applied changes | ordinary commits on your branch with an `Autotune-Candidate: <id>` trailer; reverts carry `Autotune-Revert: <id>` |

The project id is the first 16 hex characters of the SHA-256 of the repository's real Git common directory;
`autotune status --json` prints it.

## Pause and resume

`autotune pause` sets the project's pause flag. Capture continues, so episodes are still recorded, but the worker
dispatches no job of any type: reviews, proposals, evaluations, applications and reverts all wait. A job already
running continues until it ends; use `cancel` to stop it. The worker exits after it has been idle for `worker_idle_seconds` (30 by
default).

`autotune resume` clears the flag and starts a worker.

Application has two further pauses that `resume` does not clear:

- `apply_paused_reason = regression_suspected:<id>`: a review linked a severe failure to an applied candidate.
  New applications wait while a reproduce job reruns the required cases against the applied commit and its parent.
  The pause clears itself when the regression is not reproduced, or when the revert succeeds. A revert ignores this
  pause, since it exists to let the revert run.
- `git_reconciliation_required`: see [Recovery](#recovery-from-git_reconciliation_required).

## Retry and cancel

`autotune retry` requeues every blocked job (blocked on authentication, budget, environment and so on) and starts a
worker. Fix the cause first; status shows it.

`autotune cancel <job-id>` cancels a queued, waiting or blocked job at once. For a running job it requests a stop:
the worker's heartbeat sees the request within about two seconds, stops the model process and records the job as
cancelled. A cancelled evaluation never marks partial results as passed; its candidate stays in `evaluating` with
reason `cancelled` and is not applied.

## Purge

`autotune purge --scope evidence --yes` deletes the content of every evidence record. The rows stay, marked
expired, so reviews that cite them show the citation as unverifiable.

`autotune purge --scope all --yes` deletes episodes with their evidence, reviews, skill events and jobs, plus
capture errors and foreground activity. It refuses while the worker is running: pause, wait for the worker to exit
(status shows "Worker: not running"), then purge.

Neither scope touches candidates, evaluation runs or the project's flags, and neither changes your repository.
Without `--yes` the command only says what it would delete.

## Revert an applied change

`autotune revert <candidate-id>` queues a revert of an applied candidate without rerunning the reproduction. The
worker creates `autotune/revert-<id>` from your current HEAD in a separate worktree, runs `git revert` of the
applied commit there, checks that the revert touches only the candidate's own paths, and then fast-forwards your
branch with the same checks as apply (branch, HEAD, a clean checkout at those paths, no active session, not
paused). The candidate becomes `reverted`.

If later commits conflict with the revert, the worker aborts it in the worktree and removes the worktree. Your
checkout is never touched. The candidate stays `regression_suspected` with reason `revert_conflict`, and status
shows the blocker. Resolve it by hand with an ordinary `git revert <applied-commit>` on your branch, or change the
conflicting files and run `autotune revert <id>` again.

You can always undo an applied change yourself with Git: the commit is a normal commit with the
`Autotune-Candidate` trailer.

## Recovery from git_reconciliation_required

After every fast-forward (apply or revert) the worker checks that HEAD and the files at the changed paths match the
commit it merged. If they do not, it stores a short description in the `git_reconciliation_required` flag and
suspends application, and later apply and revert jobs wait with `apply_paused` until the flag is cleared. If HEAD
is not the merged commit, the job that found the mismatch fails with that code and the candidate keeps it as its
reason. If HEAD is right but files at the changed paths differ, the candidate is recorded as applied with that
reason. Nothing is stashed,
reset or rebased; the worker leaves the checkout as it found it.

To recover:

1. Read the detail: `autotune status` prints "Git reconciliation required: <detail>".
2. Inspect the checkout: `git status`, `git log -3 --format='%H %s%n%(trailers)'`. Find out whether HEAD carries the
   `Autotune-Candidate: <id>` (or `Autotune-Revert: <id>`) trailer and which files differ from that commit
   (`git diff <commit> -- <paths>`). The usual cause is an editor or another process writing to the same files
   during the merge.
3. Put the checkout in the state you want with ordinary Git commands: keep the applied commit and restore or commit
   your own edits, or undo it with `git revert`.
4. Clear the flag. There is no command for this yet, so delete the row from the project database while no worker is
   running:

   ```sh
   sqlite3 ~/.claude/autotune/<project-id>/autotune.sqlite3 \
     "DELETE FROM meta WHERE key = 'git_reconciliation_required';"
   ```

5. If the candidate is still `waiting_to_apply`, run `autotune apply <id>` again. Apply reconciles by trailer first:
   when a commit with `Autotune-Candidate: <id>` is already on the branch, it records the candidate as applied and
   makes no second commit.

## Uninstall

`autotune uninstall` lists what it would remove. `autotune uninstall --yes` pauses learning, waits for any
application or revert in progress to finish, then stops a running worker (if it does not stop, nothing is removed
and the paused setting goes back to what it was). It then removes the project's data directory (database, logs,
locks and Autotune's worktrees), and removes the `autotune/*` branches and worktrees that Autotune created,
including any made while the worker was stopping. It never touches your checkout, your other branches or `.autotune.json`, and
it prints what it removed. Applied changes stay in your history as ordinary commits.

Run `uninstall --yes` from a terminal (`python3 -m autotune uninstall --yes --project-dir /path/to/repo` in the
plugin directory), not as `/autotune uninstall --yes`. The session command times out after 20 seconds, and
uninstall can wait up to 20 seconds for the worker to stop. The same applies to `purge --scope all` after a pause.

Then, by hand:

1. Stop loading the plugin: drop `--plugin-dir /path/to/claude-autotune` from how you start Claude Code, or run
   `/plugin uninstall autotune` if you installed it from a marketplace.
2. Remove `.autotune.json` from the repository (or set `"enabled": false`) and commit that.
3. If you used Autotune in several repositories, run the uninstall in each, then remove `~/.claude/autotune/` (or
   your `AUTOTUNE_HOME`), which holds the machine-wide experiment lock.

To check by hand that nothing is left: `git branch --list 'autotune/*'`, `git worktree list`, and the data
directory path above.

## Known limitations

- A replay whose output exceeds the 8 MiB stdout cap is recorded as `invalid_output`, so a very long replay cannot
  pass.
- A candidate proposed on a detached HEAD has no target branch, and its apply job waits with `branch_changed`
  indefinitely. Cancel the job and the candidate stays unapplied.
- `InstructionsLoaded` does not fire under `claude -p`, so episodes from non-interactive sessions record exposure as
  unknown. A candidate is credited as observed active only from an interactive episode that loaded a changed path.
