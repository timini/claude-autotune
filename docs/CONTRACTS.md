# Data and process contracts

These are the records, states, formats and commands of the implemented system. Each section says what is
implemented and where the code differs from the original proposal. Machine-readable examples are synthetic and
contain no real project data, credentials or model claims. The schemas are in `schemas/`; the worker validates
against them with its own small JSON Schema subset (`autotune/jsonschema_lite.py`).

## 1. Invariants

All nine are implemented and covered by tests.

1. Every accepted capture identity maps to one episode and one initial review job.
2. Every score or factual finding cites evidence that was sent to that review.
3. A missing value is unknown, not zero or success.
4. Every candidate has one declared hypothesis and an editable scope fixed before authoring.
5. Every promotable result identifies its exact baseline, candidate, fixture, evaluator, environment and actual
   model.
6. Only the coordinator applies Git changes; models cannot self-approve or widen policy.
7. Repeating an apply job cannot create a second application commit.
8. An expired or deleted evidence reference remains visibly unavailable.
9. Git stores file history. The database stores observations, decisions and referenced SHAs.

## 2. Episode envelope

The mod sends three envelope kinds to ingress, validated against `schemas/envelope.schema.json`: `turn_start`,
`turn_complete` and `session_end`. Every envelope has `schema`, `kind`, `origin`, `session_id`, `cwd` and
`sent_at`. A `turn_complete` adds the fields below. Ingress refuses an envelope over 512 KiB.

| Field | Meaning |
|---|---|
| `session_id`, `turn_id` | Native pair used for deduplication |
| `lifecycle_id`, `lifecycle_source` | Initial load, clear, resume, compact or fork segment; does not change the deduplication identity |
| `started_at`, `completed_at`, `duration_ms` | Turn timing |
| `end_reason` | `answer`, `aborted`, `refusal`, `error` or `unknown`; `refusal` carries a category and capped explanation |
| `origin` | `foreground`, `worker`, `judge`, `author` or `replay`; only `foreground` is stored |
| `request_text`, `answer_text` | The request and the final answer, capped |
| `model`, `usage` | As reported by the engine |
| `tool_calls` | Bounded metadata of the main loop's tool calls and result excerpts |
| `skills`, `instructions` | Skills the main loop used and instruction files observed as loaded |
| `transcript_path` | Recorded, never read |
| `coverage` | Truncated fields, dropped tool calls, missing turn start, capture errors |

Ingress adds the repository state (branch, HEAD, dirty flags) by running Git before its transaction, then writes
the episode with its evidence. The proposed optional `task_id` grouping is not implemented. The episode records
`loaded_harness` from the observed skills and instructions, with exposure `unknown` when nothing was observed.

Evidence IDs (`e0001`, ...) are unique within their episode; cross-episode references always include both
identifiers. Foreign keys are enforced on every connection. Capture identity and job deduplication have uniqueness
constraints. A review row is written in the same transaction that completes its job; the model call happens before
that transaction.

## 3. Review result

See the [synthetic review](../examples/review.json) and the [review JSON schema](../schemas/review.schema.json). The
example wrapper includes its own evidence, so every reference can be checked without private files. The schema
describes the `review` object, not the wrapper. `tests.test_review.ValidationTests.test_example_review_is_valid`
checks the example.

Each of the five dimensions (`outcome`, `instruction_adherence`, `verification`, `efficiency`, `communication`)
stores `score` (0 to 4 or null), `reason` and `evidence_ids`. `strengths` and `improvements` hold a finding, its
evidence IDs and a basis, `observed` or `inferred`. The review also carries `task_stage`, `uncertainties`,
`recommendation` (`no_change`, `investigate` or `propose_candidate`), and optional `failure_signature` (a slug),
`affected_paths` and `task_family`.

The coordinator validates in this order: the schema; the `episode_id` echo; every cited evidence ID must be one that
was sent (a fictitious or omitted ID is `invalid_evidence_reference`); and the missing-outcome rule. When the
episode has no observed final answer, the coordinator forces `outcome.score` to null, sets `task_stage` to unknown
if it claimed completion, and records the adjustment. One repair attempt is allowed for invalid output or invalid
references; a second failure fails the job and keeps the rejected object as a `rejected` review row. Reviewer
model, rubric identity and call ID are set by the coordinator, never taken from model text.

## 4. Candidate, case and evaluation records

| Candidate field | Meaning |
|---|---|
| `candidate_id` | Stable identifier used in the database, branch name and commit trailer |
| `source_reviews`, `source_sessions` | Supporting reviews and their sessions |
| `hypothesis`, `effect_metric` | Behavior to change, and the criterion declared before evaluation |
| `failure_signature`, `task_family` | From the triggering review |
| `allowed_paths` | Resolved scope, constrained by enrollment |
| `change_kind` | `instruction`, `skill` or `consolidation` in practice; `hook_script`, `tool_config` and `revert` exist in the schema but no job creates them yet |
| `target_branch`, `base_commit` | Branch and exact baseline commit; set before authoring |
| `candidate_commit` | Candidate commit; set before evaluation |
| `applied_commit`, `applied_at`, `reverted_commit`, `reverted_at` | Git results, initially null |
| `evaluator_commit`, `policy_digest` | Identity of the cases tree, Autotune code and prompts, and the policy, preregistered before any run |
| `required_cases` | Case IDs selected before any run |
| `gate_json` | The gate result |
| `exposure` | Whether the applied change was observed loaded |
| `status`, `reason` | Current state and machine-readable reason |
| `branch_deleted_at` | When retention deleted the branch of a rejected or inconclusive candidate |

### Replay cases

Cases live at `.autotune/cases/<id>.json` and are read from the candidate's base commit, never the working tree.
`.autotune/**` is always protected from candidates. The format is `schemas/case.schema.json`:

| Field | Meaning |
|---|---|
| `id` | Slug equal to the file stem |
| `task_family`, `description`, `request` | Grouping, purpose, and the prompt sent to the replay |
| `kind` | `target`, `generalization`, `protected`, `negative_trigger` or `general` |
| `critical` | Default criticality for the case's graders |
| `fixture` | `files` (inline task files) and `from_commit` (paths copied from the base commit, or null) |
| `harness_paths` | Harness globs overlaid per arm, also used to select the case |
| `allowed_tools`, `allowed_bash` | Tools for the replay, and Bash allow patterns |
| `network` | `not_required` or `required` |
| `trials_per_arm`, `timeout_seconds` | 1 to 5 trials; 30 to 1800 seconds |
| `graders` | `answer_contains`, `answer_not_contains`, `answer_matches`, `file_unchanged`, `file_contains`, `file_absent`, `tool_used`, `tool_not_used`, `command` |
| `judge` | Null, or a rubric and whether a judge verdict is required |

[examples/replay-case.json](../examples/replay-case.json) is a real, valid case: a failing test run must be reported
as failing. It validates against the schema and the loader (`tests.test_cases.ExampleTest`), and its graders pass an
honest report and catch a false success claim (`tests.test_graders.ExampleCaseGraders`). Copy it to
`.autotune/cases/report-observed-test-status.json` and commit it to use it.

Required cases for a correction candidate: target and generalization cases of the same task family or named by the
candidate, critical protected cases and negative_trigger cases whose harness paths intersect the changed paths, and
up to one general case. For a consolidation: every case whose harness paths touch either skill, plus at least one
negative_trigger case.

### Runs and gate result

Each `evaluation_runs` row is one `(candidate, case, arm, trial, attempt)`. Both attempts are kept when an
infrastructure error is retried. A row records the fixture digest, harness commit, actual model, CLI build,
environment, tool grants, limits, timestamps, usage, duration, display order, grader results and outcome. `arm` is
baseline or candidate internally and maps to randomized A and B labels for judgment.

Outcomes: `success` (every grader passed), `task_failure` (a non-critical grader failed), `constraint_violation` (a
critical grader failed), `timeout`, `infrastructure_error`, `invalid_output` and `ungradeable` (a grader could not
run). The model's own claim never sets the outcome.

A gate result has `decision` (`accept`, `reject` or `inconclusive`), `reason_codes`, `required_run_ids` and eight
`checks` (integrity, static, critical, known_successes, target_effect, judgment, cost, completeness), each pass,
fail or unknown. Accept needs every check to pass. A reproducible fail in critical, known_successes or a complete
target_effect rejects; any other fail or unknown is inconclusive. Reason codes are listed in `gate.REASON_CODES`.

## 5. Job state and failure contract

```text
queued -> leased -> succeeded
                 -> retry_wait -> queued
                 -> blocked_budget / blocked_auth / blocked_environment
                 -> failed
                 -> cancelled
```

An expired lease returns the job to `queued`, or fails it at `max_attempts`. `succeeded` means the job did its
work, which may be a review recommending no change or an evaluation rejecting a candidate. It does not mean the
harness improved. A cancelled job never commits partial results.

Error codes are classified in `queue._CODE_CLASSES`: transient (retry with backoff), auth (`blocked_auth`), budget
(`blocked_budget`), environment (`blocked_environment`) and permanent (`failed`). The table gives the codes with
specific behavior. Candidate reasons that are not job errors are noted.

| Code | Behavior |
|---|---|
| `evidence_expired` | Every evidence row expired: the episode is marked unreviewable; no trace is regenerated |
| `invalid_evidence_reference` | Reject the model result after one repair attempt |
| `invalid_output` | No usable structured output after one repair attempt |
| `budget_exhausted`, `usage_limit` | Block until the next UTC day or `retry`; never shrink the required tests |
| `auth_required` | Block model calls until authentication changes and `retry` |
| `cli_missing`, `cli_incompatible`, `disk_cap` | Block as an environment problem |
| `candidate_slot_busy` | Another candidate is active; retry later |
| `checkout_dirty` | Wait; never stash or commit user work |
| `foreground_active` | Defer application while a session is known to be active |
| `branch_changed` | Defer application while another branch is checked out |
| `apply_paused` | Defer application while paused, regression-paused or reconciliation is required |
| `scope_violation` | Candidate rejected before any run (candidate reason) |
| `needs_scope_change` | Candidate held outside the automatic flow until the policy changes (candidate reason) |
| `no_change` | Author made no edit; candidate rejected (candidate reason) |
| `base_changed`, `policy_changed` | Candidate becomes stale; no rebase (candidate reasons) |
| `model_mismatch` and the other integrity codes | Gate inconclusive; the paired comparison is not promotable |
| `judge_disagreement` | Gate inconclusive; both judgments kept |
| `regression_observed`, `critical_regression` | Gate reject |
| `stochastic_regression` | Gate inconclusive |
| `revert_conflict` | Revert aborted in its worktree; the candidate stays regression_suspected |
| `git_reconciliation_required` | Application and revert suspended until the flag is cleared by hand |
| `cancelled` | Job stopped; an evaluation stays `evaluating` with reason cancelled and is never accepted |

Differences from the proposal: `capture_incomplete` is not a code; coverage gaps are sent to the reviewer as
`coverage` and input limits instead. `environment_unreproducible` is not a code; an unreproducible environment
shows up as gate integrity failures (`ancestor_customization_present`, `model_mismatch`, `cli_build_mismatch`,
`fixture_mismatch`) and makes the result inconclusive.

## 6. Configuration

Tracked `.autotune.json` holds non-secret policy. Local state and authentication stay outside the repository.
`autotune enable` writes this subset with `enabled` true and `automatic_apply` false:

```json
{
  "enabled": true,
  "automatic_apply": false,
  "trigger": "every_top_level_turn",
  "provider": "claude_code",
  "replay": "local",
  "editable_paths": [
    "CLAUDE.md",
    ".claude/rules/**",
    ".claude/skills/**",
    ".claude/agents/**"
  ],
  "executable_paths": [],
  "review_calls_per_day": 200,
  "experiment_calls_per_day": 50,
  "experiments_per_day": 2,
  "experiment_timeout_seconds": 1200,
  "consolidate_after_tool_calls": 250,
  "consolidate_after_skill_changes": 10,
  "raw_retention_days": 30,
  "result_retention_days": 180
}
```

Other keys and their defaults (`autotune/config.py` `DEFAULTS`):

| Key | Default | Meaning |
|---|---|---|
| `worker_idle_seconds`, `lease_seconds`, `max_attempts` | 30, 300, 5 | Worker exit, job lease, retry limit |
| `reviewer_model`, `author_model`, `judge_model`, `curator_model`, `replay_model` | null | Model per role; null uses the CLI default (replay then freezes the first run's actual model) |
| `claude_executable` | `claude` | CLI to run; `AUTOTUNE_CLAUDE` overrides it |
| `allow_api_key` | false | Keep API key and provider variables in model processes |
| `review_timeout_seconds`, `author_timeout_seconds`, `review_input_chars` | 300, 600, 60000 | Call limits and reviewer input budget |
| `max_data_mb`, `log_max_bytes`, `log_keep` | 1024, 5242880, 2 | Data directory cap and log rotation |
| `protected_paths` | [] | Extra globs no candidate may touch |
| `field_rules` | {} | Per file (`.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`): `editable_keys` and `executable_keys` dotted key prefixes |
| `pinned_skills`, `critical_skills` | [] | Skill directory globs never merged away or never used as merge sources |
| `consolidation_overlap_threshold` | 0.45 | Overlap score that queues a pair consolidation |

Always protected, whatever the configuration: `.autotune.json`, `.autotune/**`, `.git/**`, `.gitmodules` and
`.gitattributes` at any depth. An `editable_paths` or `executable_paths` glob that could match a protected path, or
a whole-file glob over the three field-policy files, is rejected when the file loads.

Not enforced: `experiments_per_day` is validated but nothing limits experiments by it; only
`experiment_calls_per_day` does. `trigger`, `provider` and `replay` are validated as strings and have one supported
value each.

Set `automatic_apply` to true only after validating the project's cases and policy. Candidates cannot edit this
file or the evaluator to grant themselves authority. Budgets count calls, not tokens; USD figures in usage are
estimates reported by the CLI, not subscription invoices.

`result_retention_days` applies to ordinary review and run detail and to branches of rejected and inconclusive
candidates. Applied and reverted decision records, regression signatures and protected case links are kept until
project data is deleted.

## 7. Command surface

Every command runs as `/autotune <command>` in a session (the mod runs `python3 -m autotune <command> --project-dir
<cwd>`), or directly as `python3 -m autotune <command> --project-dir DIR` from the plugin directory. The session
command times out after 20 seconds.

| Command | Behavior |
|---|---|
| `status [--json]` | Latest review, queue, model calls against budget, candidates, experiments, application and activation state, disk use, pause reasons |
| `review [EPISODE_OR_REVIEW_ID]` | Scores, findings, cited evidence and input coverage; the latest by default |
| `candidate [CANDIDATE_ID]` | List candidates, or show the hypothesis, Git diff, required cases, runs, judge verdicts, gate checks, application state and defer reason |
| `enable` | Write a default `.autotune.json` if missing; never overwrite |
| `pause`, `resume` | Stop or restart dispatch of all jobs; capture continues |
| `retry` | Requeue blocked jobs; never forces acceptance |
| `cancel JOB_ID` | Cancel a queued, waiting or blocked job; request a stop for a running one |
| `apply CANDIDATE_ID` | Queue application of an accepted candidate, also when `automatic_apply` is false |
| `revert CANDIDATE_ID` | Queue a revert of an applied candidate without reproduction |
| `consolidate` | Queue a consolidation sweep through the normal gates |
| `purge --scope evidence\|all [--yes]` | Delete evidence content, or all episodes, reviews and jobs; says what it would do without `--yes` |
| `doctor` | Check Python, Git, SQLite, the Claude CLI, enrollment and data directory permissions |
| `calibrate --set DIR [--out FILE] --live` | Run the calibration set; without `--live` exits 2 and runs nothing |
| `mutations [--set DIR] [--live --budget-calls N]` | Run the gate mutation set, simulated by default |
| `export EPISODE_ID --out FILE` | Write one sanitized episode fixture |
| `uninstall [--yes]` | Remove the project data directory and Autotune's own branches and worktrees |

`ingress` and `worker` are internal. The command hint the mod registers lists `status` to `doctor`; `calibrate`,
`mutations`, `export` and `uninstall` work but are not in the hint.

Pause and cancel are distinct: pause stops new dispatch; cancel stops one job, killing its model process group.
Setup, purging, uninstalling and any scope change need an explicit user action. Accepted scoped edits do not need
repeated approval when `automatic_apply` is true.

## 8. Model role contracts

**Reviewer** (`prompts/reviewer.md`, `schemas/review.schema.json`): receives selected evidence as a JSON DATA section
it must treat as untrusted; cites only sent evidence IDs; distinguishes unknowns; returns ratings, findings and a
recommendation. No tools, no writes, no authority to apply.

**Author** (`prompts/author.md`, `schemas/author.schema.json`): receives one hypothesis, the allowed paths, their
current content and the cited evidence. Makes a small patch in the candidate worktree with file tools only. Returns
a summary, the changed behavior and potential regressions. Cannot run commands, edit cases, graders, budgets or Git
control files.

**Judge** (`prompts/judge.md`, `schemas/judge.schema.json`): receives the request, the case rubric, and two runs as A
and B with their deterministic checks; called twice with the labels swapped. Returns a preference, tie or
insufficient evidence with citations. Never sees which arm is the candidate, the hypothesis or the author summary.
Cannot override a failed deterministic check.

**Curator** (`prompts/curator.md`, `schemas/curator.schema.json`): receives two complete skill packages and returns
merge, update or no change with a coverage map placing every trigger, procedure, exception and support file.
Returns no change when preservation cannot be shown. The edit goes through the author and the ordinary gate.

**Replay**: runs the case request in a workspace with the arm's project harness loaded, the case's tools and Bash
rules, and no permission prompts. Its answer and tool calls are graded by the coordinator; its own claims are not
trusted.
