# Autotune

A Claude Code mod that reviews every finished response, tests small improvements to the project harness against
the current one, and consolidates overlapping skills. Git holds change history and rollback. A local SQLite
database holds the evidence: reviews, experiments, outcomes and the Git commits involved.

## Status

Experimental. Milestones M0 to M7 are built: capture, reviews, candidate authoring, paired replay and the
promotion gate, Git application and revert, skill consolidation, and the calibration and mutation tooling.

All of it is tested against a scripted fake Claude CLI. None of it has release evidence from real model runs.
There are no human calibration labels, no observation period, no effectiveness pilot and no held-out study yet.
This repository makes no claim that Autotune improves outcomes. See
[release evidence](docs/RELEASE_EVIDENCE.md) for what has and has not been measured, and the
[implementation plan](docs/IMPLEMENTATION_PLAN.md) for the status of each milestone and the open items.

Tested with Claude Code 2.1.306 on macOS. It needs Python 3.11 or later (standard library only) and Git. The mod
API is early access and may change between Claude Code releases.

## What it does

1. After each completed top-level response, the mod sends a bounded, redacted record of the turn to a short
   ingress process, which stores it and queues a review. A detached worker does the rest in the background.
2. A tool-free reviewer rates the turn on five dimensions, citing evidence ids. A missing outcome is recorded as
   unknown, not as success.
3. When a review shows an observed failure, or the same failure recurs across sessions, a separate author process
   writes one small candidate change in a disposable Git worktree, limited to the enrolled editable paths.
4. The candidate and the current harness are replayed on the same task fixtures from `.autotune/cases/`, and a
   deterministic gate accepts, rejects or reports inconclusive. Known successes must be kept.
5. An accepted candidate is applied as a fast-forward of your branch only when the checkout is clean at its
   paths, HEAD has not moved, and no session is active. By default it waits for you to apply it.
6. Overlapping skills are merged through the same authoring, gate and Git path. A merge must keep every
   trigger, exception and support file. Nothing is deleted for low use.

## Install

From a local checkout:

```sh
git clone <this repository> /path/to/claude-autotune
cd /path/to/repo && claude --plugin-dir /path/to/claude-autotune
```

From the marketplace, at the prompt of a Claude Code terminal session (answer `y` to add the marketplace, then pick a scope):

```
/plugin install autotune --marketplace timini/claude-autotune
```

From a shell:

```sh
claude plugin install autotune --marketplace timini/claude-autotune
```

## Quick start

1. Enroll the repository. This writes `.autotune.json` with `automatic_apply` set to false. Commit it.

   ```sh
   cd /path/to/claude-autotune && python3 -m autotune enable --project-dir /path/to/repo
   ```

   Inside a session with the plugin loaded, `/autotune enable` does the same.

2. Add replay cases under `.autotune/cases/<id>.json` and commit them. Without cases, candidates end as
   `inconclusive` with reason `no_required_cases`. [examples/replay-case.json](examples/replay-case.json) is a
   valid case; the format is in [docs/CONTRACTS.md](docs/CONTRACTS.md) section 4.

3. Start Claude Code with the plugin and work as usual. `/autotune status` shows the queue, recent episodes,
   reviews, candidates, model call counts, disk use and anything that is waiting, with the reason.

4. Inspect a review or a candidate with `/autotune review` and `/autotune candidate <id>`. Apply an accepted
   candidate with `/autotune apply <id>`.

Evidence lives in `~/.claude/autotune/<project-id>/` (set `AUTOTUNE_HOME` to move it), never in the repository.

## Commands

Every command runs as `/autotune <command>` inside a session, or from the plugin directory as
`python3 -m autotune <command> --project-dir /path/to/repo`. `/autotune` with no argument runs `status`. The
`/autotune` hint in the session lists the commands down to `doctor`; `calibrate`, `mutations`, `export` and
`uninstall` work the same way but are not in the hint. Run `uninstall` from a terminal: the session command
times out after 20 seconds, and uninstall can wait that long for the worker to stop.

| Command | What it does |
|---|---|
| `status [--json]` | Queue, recent episodes, latest review, candidates by status, experiments in progress, application and activation state, model calls today against budget, disk use, pause reasons |
| `enable` | Write a default `.autotune.json` if there is none (`automatic_apply` false); never overwrites |
| `pause` / `resume` | Stop or restart dispatching jobs; capture continues while paused |
| `retry` | Requeue blocked jobs (authentication, budget, environment) and start a worker |
| `cancel JOB_ID` | Cancel a queued or blocked job, or ask a running job to stop |
| `review [EPISODE_OR_REVIEW_ID]` | Show a review: scores, findings, cited evidence, input coverage. Default: the latest |
| `candidate [CANDIDATE_ID]` | List candidates, or show one: hypothesis, Git diff, required cases, runs, gate checks, application state |
| `apply CANDIDATE_ID` | Queue application of an accepted candidate, with the same rechecks as automatic application |
| `revert CANDIDATE_ID` | Queue a Git revert of an applied candidate, prepared in a separate worktree |
| `purge --scope evidence\|all [--yes]` | Delete local evidence content, or all episodes, reviews and jobs. Without `--yes` it only says what it would delete |
| `doctor` | Check Python, Git, SQLite, the Claude CLI, enrollment and data directory permissions |
| `consolidate` | Queue a skill consolidation sweep now |
| `calibrate --set DIR [--out FILE] --live` | Run the reviewer and judge over a labelled set and report agreement. Without `--live` it runs nothing and exits 2. See [calibration/README.md](calibration/README.md) |
| `mutations [--set DIR] [--live --budget-calls N]` | Run the gate mutation set. Simulated by default (no model calls); `--live` runs real replays and needs a call budget |
| `export EPISODE_ID --out FILE` | Write one episode as a sanitized fixture and print what was removed. Refuses expired evidence |
| `uninstall [--yes]` | Remove the project data directory and the `autotune/*` branches and worktrees Autotune created. Without `--yes` it only lists them |

`worker` and `ingress` are internal entry points used by the mod.

## Safety defaults

- `automatic_apply` is false. An accepted candidate waits until you run `apply`. Set it to true in
  `.autotune.json` only after the project's cases and policy are in place.
- Candidates may edit only `editable_paths` (by default `CLAUDE.md`, `.claude/rules/**`, `.claude/skills/**`,
  `.claude/agents/**`). `.autotune.json`, `.autotune/**` and `.git/**` are always protected. Symlinks, binary
  files and submodules are rejected. New executable files and hook or MCP commands are rejected unless
  `executable_paths` and `field_rules` allow them.
- Autotune never stashes, resets, rebases or pushes. It applies by fast-forward only, and leaves your uncommitted
  work untouched.
- Reviews, judging and curation run tool-free under `--safe-mode`. An inherited `ANTHROPIC_API_KEY` is removed
  from model processes unless `allow_api_key` is true, so a review does not silently switch to API billing.
- Daily call budgets (`review_calls_per_day` 200, `experiment_calls_per_day` 50) cap model use. Exhausted
  budget leaves work pending; it never shrinks a test suite.
- Replays run on your machine with your user's access. Worktrees and temporary workspaces are not a security
  boundary.

How to pause, cancel, purge, revert, recover and uninstall: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Tests

```sh
python3 -m unittest discover -s tests -t .   # Python worker, fake CLI only, no model calls
claude plugin test .                          # native mod tests
claude plugin validate .
```

Paid checks (the plugin eval suite in `evals/` and the live mutation run) run only from the manual
`.github/workflows/paid-tests.yml` workflow.

## Documentation

| Document | Purpose |
|---|---|
| [Product requirements](docs/PRD.md) | Scope, user behavior, requirements, defaults, acceptance criteria |
| [Architecture](docs/ARCHITECTURE.md) | Mod, worker, SQLite, Git, concurrency, model processes |
| [Data and process contracts](docs/CONTRACTS.md) | Records, states, case format, reason codes, configuration, commands |
| [Evaluation and test strategy](docs/EVALUATION.md) | Judge calibration, paired replay, regression gates, experiments |
| [Implementation plan](docs/IMPLEMENTATION_PLAN.md) | Milestones, exit criteria with their evidence, open items |
| [Operations](docs/OPERATIONS.md) | Pause, cancel, purge, revert, recovery, uninstall |
| [Release evidence](docs/RELEASE_EVIDENCE.md) | What has been measured, and what is planned |
| [Prior art and research](docs/RESEARCH.md) | What already exists, primary sources, limitations |
| [Decisions](docs/DECISIONS.md) | Settled product choices |

## Prior art

[RHO](https://github.com/wbopan/retro-harness) is the closest precedent for retrospective harness optimization.
[Tigerless AutoHarness](https://github.com/tigerless-labs/autoharness) is the direct inspiration for continual
skill extraction and consolidation. [Hermitd](https://github.com/gtapps/hermitd) separates reflection from
evidence judging, and [Kayba AutoHarness](https://github.com/kayba-ai/autoharness) connects harness changes to
benchmark results. Autotune combines these into a bounded, per-response workflow with a persistent record of
attempted changes and observed regressions. It does not claim that self-improving harnesses are new.

## License

MIT. See [LICENSE](LICENSE).
