# Release evidence

The sanitized evidence summary for a release (docs/EVALUATION.md section 11). Fill in one copy per release. A row
that has not been run says **planned**. A row with only fake-CLI, simulated or synthetic evidence says so.
Simulated and synthetic results are never calibration, gate or effectiveness evidence.

Release: experimental, unreleased. Values below were recorded on 21 October 2026 on the development machine,
against the uncommitted working tree after cycle 3. Re-run them on the release commit.

## Build and environment

| Item | Value | Source |
|---|---|---|
| Release commit | planned (not tagged) | `git rev-parse HEAD` |
| Claude Code CLI | 2.1.306 | `claude --version` |
| Platforms tested locally | macOS 26.6.2 (Darwin 25.6.0, arm64) | development machine |
| Platforms in CI | ubuntu-latest and macos-latest with Python 3.11 and 3.13 all pass (run 39685368942 on commit b59cfb1, 21 October 2026) | GitHub Actions |
| Python | 3.15.0 and 3.11.16: full suite passes on both | `python3 --version` |
| Git, SQLite | Git 2.50.1; SQLite library 3.54.1 (Python 3.15); `sqlite3` CLI 3.51.0 | `git --version`, `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"` |

## Mechanics

| Check | Result | How to reproduce |
|---|---|---|
| Unit and fake-CLI integration tests | 879 passed on Python 3.15.0 (274 s) and 879 passed on Python 3.11.16 (274 s); no model calls | `python3 -m unittest discover -s tests -t .` |
| Native mod tests | 28 of 28 passed | `claude plugin test .` |
| Plugin validation | passed with one warning: ICON_MISSING (plugin directory check only). The manifest has an author and the MIT license; the marketplace manifest validates | `claude plugin validate .` |
| Type check of hooks/register.ts | clean (local only; CI skips it, see the comment in ci.yml) | `npx -y -p typescript@5.6.3 tsc -p <tsconfig including claude-code.d.ts and hooks/>` |
| JSON validity of schemas and examples | passed | the "JSON files parse" step in ci.yml |
| Retention | passed (fake CLI): evidence expiry, result detail expiry, candidate branch deletion | `python3 -m unittest tests.test_store_retention tests.test_cli_cycle3.CandidateBranchRetentionTests` |
| Export | passed (fake CLI): redaction, path replacement, session id hashing, refusal of expired evidence | `python3 -m unittest tests.test_cli_cycle3.ExportTests` |
| Uninstall | passed (fake CLI): removes only Autotune's data, branches and worktrees; never signals the worker during an apply | `python3 -m unittest tests.test_cli_cycle3.UninstallTests` |
| Install with `--plugin-dir` | passed live (M0 run, `claude -p --plugin-dir <repo>`) | docs/IMPLEMENTATION_PLAN.md, M0 |
| Install from a marketplace | published as the `claude-autotune` marketplace in this repository; installed and enabled from GitHub into an empty `CLAUDE_CONFIG_DIR` on 22 October 2026 (Claude Code 2.1.306) | `/plugin install autotune --marketplace timini/claude-autotune` |

## Capture latency

Target: p95 at most 150 ms and p99 at most 500 ms for each blocking hook call. Measured with a 480 KiB envelope,
3,000 tracked files and a worker running, 60 turns; the single-ingress row is 200 runs with no worker.

Python 3.15.0, recorded 2026-10-21T21:41Z:

| Measurement | p50 | p95 | p99 | Meets target |
|---|---|---|---|---|
| turn_start ingress | 73.1 ms | 76.8 ms | 78.8 ms | yes |
| turn_complete ingress | 122.0 ms | 126.4 ms | 129.5 ms | yes |
| Sum per turn | 196.0 ms | 199.9 ms | 203.9 ms | not a per-call target; reported |
| Single ingress, no worker | 72.5 ms | 73.4 ms | 74.3 ms | yes |

Python 3.11.16, recorded 2026-10-21T21:45Z while other test runs were using the machine: turn_start p95 83.3 ms,
turn_complete p95 135.0 ms, per turn p95 218.4 ms, single ingress p95 80.7 ms. Each call still meets the target.

Decision: the 150 ms target applies to each blocking hook call, and both calls meet it. The per-turn sum of
196 ms p95 (the run the decision was taken on) is reported and accepted for the experimental release. Later runs
are listed above.

Source: `benchmarks/results/capture-latency-turn.json` and `capture-latency.json`, written by
`tests/test_integration_benchmark.py` on every suite run. `benchmarks/results/` is ignored by Git, so copy the JSON
into the release evidence rather than linking to it.

## Judge calibration

| Item | Value |
|---|---|
| Labelled examples | **planned**: 120 independently labelled examples with two human labellers and adjudication do not exist |
| Synthetic seed set | 16 examples in `calibration/seed/` (10 review, 6 pairwise), covering every probe; labels set by construction |
| Seed set through the fake CLI with scripted outputs (synthetic, not evidence) | 22 calls; 7 of 7 labelled reviews agree; kappa 1.0 per dimension; 22 of 22 outputs with valid references; 0 of 2 fabricated successes accepted; 6 of 6 pairs position-consistent; 2 abstentions. These figures follow from the scripted outputs and say nothing about the reviewer. Source: `tests.test_calibrate.SeedRunFakeCliTests.test_perfect_scripted_outputs` |
| Pass/fail agreement (Wilson 95% interval) | planned |
| Weighted kappa per dimension | planned |
| Valid evidence reference rate | planned |
| Accepted fabricated-success examples | planned |
| Pairwise position consistency | planned |
| Abstention rate, subgroup results | planned |
| Threshold verdict | insufficient sample (fewer than 120 adjudicated, non-ambiguous, non-synthetic calibration-split examples) |

Reproduce: `autotune calibrate --set <dir> --out <file> --live` (real model calls).

## Gate mutation set

| Item | Value |
|---|---|
| Set | 40 harmful (10 categories, 4 each), 9 useful, 6 neutral mutations in `benchmarks/mutations/` against `benchmarks/fixture-harness/` |
| Simulated mode (real static checks and gate, scripted runs; not release evidence) | PASSED every release criterion |
| Harmful false accepts | simulated: 0 of 40; live: **planned** |
| Harmful rejected and abstained | simulated: 39 rejected (14 by static checks, 25 by the gate), 1 abstained; live: **planned** |
| Exact Clopper-Pearson 95% upper bound on the false-accept rate (two-sided) | simulated: 0.0881 (0 of 40); live: **planned** |
| Useful accepted rate (target at least 80%) | simulated: 9 of 9 (100%); live: **planned** |
| Neutral reported as improvement | simulated: 0 of 6; live: **planned** |
| Each harmful mutation caught by the check it names | simulated: met |
| Weakened gate is detected | passed (fake CLI): `tests.test_mutations.WeakenedGateTest` (disabled known-successes, static, merge and consolidation checks each fail the run) |

Simulated results show that the gate logic and static checks behave as designed on plausible run outcomes. They
are not a measurement of the gate against real model behaviour, and the false-accept bound above is a property of
40 scripted cases, not of the gate in use.

Reproduce: `autotune mutations --set benchmarks/mutations` (simulated); `autotune mutations --live --budget-calls N`
through `.github/workflows/paid-tests.yml`.

## Plugin eval suite

| Item | Value |
|---|---|
| Suite | `evals/`: normal-response, tool-passthrough, command-surface |
| Result | **planned**; not run (real model calls) |

Reproduce: `claude plugin eval . --no-publish`.

## Live vertical slice

| Item | Value |
|---|---|
| Live haiku vertical slice (review, author, paired replay, gate, apply in a scratch repository) | Run on 21 October 2026 with Claude Code 2.1.306, every role set to `haiku` (resolved `claude-haiku-5-5`), `automatic_apply` on in the scratch repository only. One synthetic turn: the user asked for a final line `RESULT: <number> failed` and the answer omitted it. Three committed cases: two targets graded on that line, one critical protected case requiring Bash |
| Run 1 | review and author succeeded; evaluate stopped at `no_required_cases`, because the reviewer named the task family `test-run-reporting` and the cases use `test-reporting`. Fixed by giving the reviewer the families of the committed cases (`known_task_families`, `tests.test_review_families`). 2 calls, $0.0043 |
| Run 2 | review succeeded (`propose_candidate`); the author added a general rule to CLAUDE.md ("when the user asks for a specific output format, follow it exactly"); 6 paired replays ran with model identity recorded on every run; the gate rejected with `target_not_met`: both arms failed both target cases (the case requests never ask for the format, so the rule cannot help), the protected case passed in both arms. Apply did not run, so the user checkout was unchanged. 8 calls, $0.0069 |
| Live total | 10 calls, $0.0112, plus about $0.002 for three earlier profile probes. Apply, revert and recovery were not exercised live; they call no model and are covered by the fake-CLI tests |

## Replay fixtures and commits

| Item | Value |
|---|---|
| Example replay case | `examples/replay-case.json`, a valid case (`tests.test_cases.ExampleTest`, `tests.test_graders.ExampleCaseGraders`) |
| Fixture harness for the mutation set | `benchmarks/fixture-harness/` at the release commit. Simulated mode builds a fresh temporary repository from it on every run, so there is no separate fixture commit to record |
| Candidate and applied commits from a live run | **planned** |

## Operational pilot and effectiveness

| Item | Value |
|---|---|
| Report-only observation period | **planned** |
| Narrowly scoped automatic operation on a test repository | **planned** |
| One reproducible applied improvement | fake CLI only (`tests.test_e2e_cycle2`, `tests.test_evaluate.GateOutcomeTest`); live: **planned** |
| One reproducible prevented regression | fake CLI only (`tests.test_e2e_cycle2`, `tests.test_evaluate.GateOutcomeTest.test_removed_verification_is_rejected_by_a_protected_critical_case`); live: **planned** |
| Paired effectiveness pilot (20 tasks, four families) | **planned** |
| Held-out effectiveness study | **planned**; does not exist, and no effectiveness claim is made |

## Usage totals

| Item | Value |
|---|---|
| Model calls, tokens and cost for all live runs in this release (reviews, judges, authors, curators, replays, failed experiments) | **planned**; no live run is recorded in this document |

## Known limitations

- A replay whose output exceeds the 8 MiB stdout cap is recorded as `invalid_output`.
- A candidate proposed on a detached HEAD has no target branch and its apply job waits with `branch_changed`
  indefinitely.
- `InstructionsLoaded` does not fire under `claude -p`, so `verify_active` needs positive exposure from a real
  interactive episode.
- Worktrees and replay workspaces are not a security boundary: approved hooks and scripts in replays run with the
  local user's access.
- There is no command to clear `git_reconciliation_required`; docs/OPERATIONS.md gives the manual step.
- `experiments_per_day` is not enforced; only the daily call budgets limit experiments.
- The mod API is early access and may change between Claude Code releases.

The full list of open items is in docs/IMPLEMENTATION_PLAN.md section 3.
