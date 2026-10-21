# Decisions and remaining checks

These decisions reflect the requested product. They are not open questions for the initial implementation.

| Decision | Rationale |
|---|---|
| **Autotune**, public `timini/claude-autotune` repository | Selected name and publication scope |
| Review every completed top-level response | A pause within a session is a learning boundary; session end alone is too infrequent |
| Use a native Claude Code mod | Observe turn and skill events directly; keep lifecycle integration small |
| Separate background worker | Reviews and experiments can outlive the parent process without blocking the user |
| Python standard library and SQLite | Small local dependency footprint; durable evidence and queue without a server |
| Git for all harness history | No custom versions, snapshot registry, or rollback store |
| DB references Git commits | Connect ratings and results to actual changes without duplicating file history |
| Claude Code first, subscription authentication | Review, judge, and edit through the tool the user already wants to use |
| Separate review, author, and judge processes | Prevent shared-context self-certification; acknowledge remaining model-family correlation |
| Whole project harness may change | Existing instructions, skills, approved hooks/scripts, and tool config are in scope |
| Protect controller and evaluation policy | A candidate cannot weaken its own acceptance conditions |
| One candidate experiment at a time | Bound cost, avoid competing edits, keep attribution understandable |
| Local replays only | Disposable worktrees and controlled commands; no Docker dependency |
| Automatic application inside enrolled scope | No routine per-change permission loop once evaluation and application checks pass |
| Do not modify a dirty or changed target checkout | Preserve user work and the validity of the tested base |
| Consolidation uses the ordinary candidate path | No separate untested merging/pruning mechanism |
| No zero-regression guarantee | Known tests provide evidence; unseen-task failures remain possible |
| No hosted service/dashboard in v1 | Native status and inspection commands are sufficient initially |

## Remaining engineering checks

These are implementation experiments, not requests for more product decisions.

1. Verify event identity and clear/resume behavior on the tested CLI build; preserve deduplication across reloads.
2. Measure short-process capture overhead and detached-worker lifetime on macOS/Linux.
3. Verify subscription authentication with isolated replay configuration and confirm the effective billing source.
4. Verify which instruction, hook, skill, and tool-config changes become active without a new conversation. Preserve exposure uncertainty where observation is incomplete.
5. Demonstrate that the replay runner loads the entire intended harness and excludes unrelated customization.
6. Validate local permission/command restrictions against ordinary accidental access. Local execution does not provide an adversarial security boundary.
7. Establish real judge agreement, mutation rejection, runtime/cost distributions, and practical sample sizes.
8. Test concurrent editor/terminal races around Git application. Cooperating Autotune locks do not lock unrelated tools.

## Explicitly deferred

Native Windows support, additional model providers, cross-project learning, cloud workers, embedding retrieval, multi-candidate search, stronger process/container isolation, and a UI dashboard. Revisit only when evidence justifies their complexity.

Source support and dates are in [Research](RESEARCH.md); acceptance criteria are in [Evaluation](EVALUATION.md).
