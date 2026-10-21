# Data and process contracts

These are proposed interfaces for implementation. They are not executable commands or an installed plugin. Machine-readable examples are synthetic and deliberately omit real project data, credentials, and model claims.

## 1. Invariants

1. Every accepted capture identity maps to one episode and one initial review job.
2. Every score or factual finding cites evidence available to that review.
3. A missing value is unknown, not zero or success.
4. Every candidate has one declared hypothesis and an editable scope fixed before authoring.
5. Every promotable result identifies its exact baseline, candidate, fixture, evaluator, environment, and actual model.
6. Only the coordinator applies Git changes; models cannot self-approve or widen policy.
7. Repeating an apply job cannot create a second application commit.
8. An expired or deleted evidence reference remains visibly unavailable.
9. Git stores file history. The DB stores observations, decisions, and referenced SHAs.

## 2. Episode envelope

| Field | Meaning |
|---|---|
| `episode_id` | Stable local UUID or equivalent identifier |
| `session_id`, `turn_id` | Native pair used for deduplication; stability verified in M0 |
| `lifecycle_id` | Initial load/clear/resume segment metadata; does not change deduplication identity |
| `task_id` | Optional grouping across turns; unknown is allowed |
| `completed_at` | UTC boundary timestamp |
| `end_reason` | `answer`, `aborted`, `refusal`, `error`, or `unknown` |
| `origin` | `foreground`, `worker`, `judge`, `author`, or `replay`; only foreground enters learning |
| `repository` | Local project identity, worktree, branch, observed HEAD, dirty flags |
| `loaded_harness` | Observed instruction/skill paths and available content digests; unknown exposure explicit |
| `evidence` | Array of stable episode-local evidence IDs, roles/types, excerpts, provenance |
| `coverage` | Missing/truncated fields, capture cap, unsupported media, boundary uncertainty |

The mod supplies what it observes. It must not invent a task completion label. The worker validates absolute paths locally but exports only deliberately sanitized references. Evidence IDs are only unique within their episode; cross-episode references always include both identifiers.

Foreign-key relationships should be enforced in SQLite. Enable foreign keys on every connection. Use uniqueness constraints for capture identity and job deduplication. A write to `reviews` and completion of its job occur in one transaction; any model request happens before that transaction.

## 3. Review result

See [synthetic review](../examples/review.json) and [review JSON schema](../schemas/review.schema.json). The example wrapper includes its own evidence, so every reference can be checked without private files. The schema describes the `review` object, not the wrapper.

Each dimension stores `score`, `reason`, and `evidence_ids`. Null scores have a reason such as insufficient outcome evidence. `strengths` and `improvements` contain a concise finding, evidence IDs, and an explicit evidence basis: `observed` or `inferred`. Inferred findings can suggest investigation; they cannot establish a historical fact.

`recommendation` is `no_change`, `investigate`, or `propose_candidate`. A proposal recommendation is not acceptance. Reviewer metadata includes the actual model, rubric Git commit, and model-call identity, set by the coordinator rather than trusted from model text.

JSON Schema validates shape. Application validation additionally resolves references, verifies the episode association, enforces length limits, checks that claimed excerpts exist, and verifies all required dimensions appear exactly once. A structurally valid hallucination still fails evidence validation.

## 4. Candidate and evaluation records

| Candidate field | Meaning |
|---|---|
| `candidate_id` | Stable identifier used in DB, branch name, and commit trailer |
| `source_reviews` | References to supporting reviews and tasks |
| `hypothesis` | Specific behavior to change and why |
| `effect_metric` | Observable criterion declared before evaluation |
| `allowed_paths` | Resolved allowed scope, constrained by enrollment |
| `change_kind` | Instruction, skill, consolidation, hook/script, or tool config |
| `base_commit` | Exact baseline Git commit; required before authoring |
| `candidate_commit` | Candidate Git commit; required before evaluation |
| `applied_commit`, `reverted_commit` | Git results, initially null |
| `evaluator_commit`, `policy_digest` | Identity of trusted evaluation code/configuration |
| `required_cases` | Complete case set and trial policy |
| `status`, `reason` | Current state and machine-readable explanation |

Each evaluation row describes one `(candidate, case, arm, trial, attempt)`. Retain both attempts when a declared infrastructure retry occurs. Include the task fixture commit/digest, harness commit, actual model, CLI build, dependency/toolchain identity, tool grants, limits, timestamps, measured usage, artifact references, deterministic results, and judge result. `arm` is baseline/candidate internally but maps to randomized A/B labels for judgment.

Example outcome categories: `success`, `task_failure`, `constraint_violation`, `timeout`, `infrastructure_error`, `invalid_output`, and `ungradeable`. Success means required external checks passed. The model saying it succeeded does not set this field.

A gate result contains `decision`, `reason_codes`, `required_run_ids`, and `checks`. Each required check is pass/fail/unknown. `accept` requires all mandatory checks to pass. An unknown required check yields `inconclusive`.

The [synthetic replay case](../examples/replay-case.json) illustrates a failure-driven case and its independent outcome requirements. It is a design example, not an installed runnable case or a validated benchmark.

## 5. Job state and failure contract

```text
queued -> leased -> succeeded
                 -> retry_wait -> queued
                 -> blocked_budget / blocked_auth / blocked_environment
                 -> failed
                 -> cancelled
```

An expired lease returns to eligible queued work after reconciliation. `succeeded` means the job completed its function, which may be a review recommending no change or an evaluation rejecting a candidate. It does not mean a harness was improved.

| Reason code | Required behavior |
|---|---|
| `capture_incomplete` | Review with explicit limits; restrict factual claims |
| `evidence_expired` | Mark unreviewable/unverifiable; do not regenerate a fictitious trace |
| `invalid_evidence_reference` | Reject model result; at most a bounded repair attempt |
| `budget_exhausted` | Keep pending; do not shrink required tests |
| `auth_required` | Stop model retries until authentication changes |
| `model_mismatch` | Invalidate the paired comparison |
| `scope_violation` | Reject candidate before execution |
| `needs_scope_change` | Hold proposal outside automatic flow |
| `environment_unreproducible` | Report limited evidence; no automatic acceptance |
| `base_changed` | Mark candidate stale; regenerate/re-evaluate from current base |
| `checkout_dirty` | Wait; never stash or commit unrelated user work |
| `foreground_active` | Defer application while known user work is running |
| `judge_disagreement` | Inconclusive; preserve both judgments |
| `regression_observed` | Reject or queue reproduction according to evidence strength |
| `git_reconciliation_required` | Suspend application and inspect actual Git state |

## 6. Proposed configuration

Tracked `.autotune.json` holds non-secret product policy. Local state and authentication stay outside the repository. Initial fields:

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

This is a starting enrollment example, not a limitation on product scope. Explicitly enrolled hook/scripts/tool settings add paths, approved executables, and field-level configuration rules. Set `automatic_apply` after validating the project's evaluator and policy. Candidates cannot edit this file or the trusted evaluator to grant themselves authority.

Model selections are resolved during setup and stored in non-secret configuration using actual supported identifiers. Avoid a hard-coded assumption that an alias always identifies the same model. Reserve call/token/time budgets before dispatch. USD equivalents, when available, are estimates rather than subscription invoices.

`result_retention_days` applies to ordinary review and run detail. Applied/reverted decision links, confirmed regression signatures, and active protected-case definitions remain until project removal or explicit deletion. A transcript retention deadline does not deactivate its validated regression case.

## 7. Proposed command surface

| Command | Intended behavior |
|---|---|
| `/autotune status` | Latest review, queue, usage, application and activation state |
| `/autotune review <episode>` | Scores, strengths, improvements, evidence coverage |
| `/autotune candidate <id>` | Hypothesis, Git diff, required cases, actual results |
| `/autotune enable` | Validate/setup project enrollment and start capture |
| `/autotune pause` | Stop new learning dispatch and automatic application; keep existing records |
| `/autotune resume` | Resume the existing enrollment |
| `/autotune retry` | Recheck eligible blocked work; never force acceptance |
| `/autotune consolidate` | Queue a consolidation analysis through normal gates |
| `/autotune revert <candidate>` | Prepare and validate an ordinary Git revert |
| `/autotune purge` | Explicitly remove selected local evidence with visible scope |

Pausing and cancelling are distinct: pause prevents new dispatch; cancel terminates the selected running process group. Setup, destructive evidence purging, and any scope expansion need an explicit user action. Ordinary scoped accepted edits do not need repeated approval. Final command syntax should be verified against native command registration during M0.

## 8. Model role contracts

**Reviewer:** receive selected evidence and relevant constraints; cite observations; distinguish unknowns; return ratings, strengths, improvements, and a recommendation. No tools, writes, policy changes, or authority to apply.

**Author:** receive one hypothesis, allowed paths, baseline files, and visible development cases. Make a small targeted patch. Explain changed behavior and potential regressions. Do not alter hidden cases, graders, budgets, or Git control files. No candidate hook execution during authoring.

**Judge:** receive A/B outputs, matched task context, external checks, and a fixed rubric. Identify preference/tie/insufficient evidence and cite artifacts. Do not receive candidate identity or a persuasive author summary. A judge cannot override a failed hard check.

**Curator:** inspect complete skill packages and propose one consolidation with an explicit coverage map. Preserve distinct triggers, exceptions, and support files. Return no change when preservation cannot be established. Route edits through the author and ordinary evaluation gate.
