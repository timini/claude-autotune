# Product requirements

Status: implementation specification. Research date: 21 October 2026. Product name: **Autotune**.

## 1. Problem and intended outcome

Claude Code sessions contain useful evidence about the agent's behavior: corrections from the user, successful procedures, repeated mistakes, unnecessary tool calls, missing tests, and skills that overlap. That evidence usually disappears into transcripts or accumulates as increasingly long instructions.

Autotune should turn this evidence into tested improvements to the project harness. The user should continue working normally while a background process reviews the completed response, records its reasoning, and evaluates useful changes. Accepted changes should preserve earlier successes, reduce repeated failures, and keep skills coherent.

A stored rating is an observation, not proof that a change will help. The product must connect an observation to a concrete hypothesis, an actual Git change, and comparative evaluation results.

## 2. Product decisions

| Area | Decision |
|---|---|
| Trigger | Every top-level completed response; no session-end-only trigger or idle debounce |
| Scope | Project instructions, skills and supporting files, agents, approved hooks/scripts, and tool configuration |
| Automation | Qualifying changes apply automatically after evaluation; explicit project enrollment defines the scope |
| Models | Separate Claude Code processes for review, editing, and judging; subscription authentication first |
| Work allocation | One candidate experiment at a time; background reviews continue within budget |
| Consolidation | Check related skills during learning; periodically examine broader overlap |
| History | Standard Git commits, branches, diffs, and revert |
| Evidence | Local SQLite database, with Git SHA references |
| Replays | Local disposable Git worktrees and controlled commands; Docker is not required |
| Distribution | Public repository `timini/claude-autotune`; installable plugin is a later deliverable |
| Initial support | Claude Code CLI on macOS and Linux; compatibility baseline 2.1.306 |

Separate processes avoid sharing conversational context. They do not make models from the same family statistically independent.

## 3. Users and core journeys

### Daily development

The user completes a coding response. Autotune records the intent, relevant constraints, actions, observable results, and harness files that were actually loaded. A short background review identifies strengths and improvements, or reports insufficient evidence. A quiet status indicator shows pending work without adding another message to every response.

### Repeated error

Two independent tasks show the agent editing generated files instead of their source. Autotune proposes a narrowly scoped instruction, creates a regression case that checks the source file, and replays both the current and proposed harness from the same fixture. The candidate must also preserve unrelated successful behavior. A qualifying change is committed when the target branch and checkout meet the application checks.

### Successful procedure

The agent discovers an effective repository-specific diagnostic procedure. Autotune checks whether an existing skill already covers it. It adds only the missing procedure, or records the success without editing. The known success becomes a candidate regression case after its outcome has been independently checked.

### Skill consolidation

Several skills cover variants of the same workflow. Autotune proposes a merge that preserves distinct trigger conditions, exceptions, references, scripts, and templates. Evaluation covers the union of the original skills' tasks and negative trigger cases. A shorter skill that loses an important exception fails.

### Later regression

A later session appears to violate behavior preserved by a prior change. Autotune links the observation to the relevant applied commit, attempts reproduction, and tests a Git revert where possible. Confirmed regressions can trigger a revert under the same clean-checkout rules. An unverified low rating alone does not revert work.

## 4. Functional requirements

### Capture and review

| ID | Requirement | Acceptance condition |
|---|---|---|
| F01 | Observe every top-level response completion | Each unique session/turn boundary produces one durable episode record, or an explicit capture error |
| F02 | Include incomplete outcomes | Aborted, refused, and error turns are recorded; task success can remain unknown |
| F03 | Prevent recursive learning | Worker, judge, authoring, and replay sessions do not enqueue their own reviews |
| F04 | Review asynchronously | No model request runs in the foreground event handler |
| F05 | Use evidence | Every non-null rating and factual finding references captured evidence; nonexistent references invalidate the review |
| F06 | Preserve strengths | Reviews record successful behavior as well as failures; useful successes can enter the regression bank |
| F07 | Recover context boundaries | Clear, resume, branch, restart, compaction, duplicate events, and truncated capture have explicit handling |
| F08 | Show uncertainty | Missing outcomes, truncated inputs, and inferred intent are visible; the judge cannot invent completion |

An episode is one completed response plus the context needed to understand it. A task may span many episodes. Task completion must not be inferred merely because a response ended.

### Learning and changes

| ID | Requirement | Acceptance condition |
|---|---|---|
| F09 | Produce bounded hypotheses | Each candidate addresses one stated failure or improvement and identifies the expected observable effect |
| F10 | Use accumulated evidence | Consult related prior reviews, rejected attempts, successful changes, and regressions before proposing |
| F11 | Support a no-change result | Good performance, insufficient evidence, and an existing adequate rule can all end a review without an edit |
| F12 | Edit through Claude Code | A separate authoring process receives an explicit scope and writes only within a disposable worktree |
| F13 | Cover the project harness | Existing project instructions and skills may be edited; learning is not restricted to generated skills |
| F14 | Protect the evaluator | Candidates cannot change Autotune's controller, rubrics, gates, trusted graders, budget, or editable-path policy |
| F15 | Evaluate before applying | Static validation, paired outcome tests, and the applicable regression suite all pass |
| F16 | Apply with Git | Accepted patches create ordinary Git commits; the database references commits without storing a parallel harness history |
| F17 | Preserve user work | Dirty checkouts, changed HEAD, conflicts, or known active turns defer application; no automatic stash, reset, or forced checkout |
| F18 | Track effective use | Record `applied` separately from `observed_active`; a committed file may not yet be loaded by an existing conversation |
| F19 | Undo through Git | Reverts retain the original evidence and have their own commit; conflicts stop automatic application |

A directly observed failure or explicit user correction may justify a candidate immediately. A speculative efficiency improvement normally needs recurrence across at least two independent tasks. Neither threshold bypasses evaluation.

### Skills and operation

| ID | Requirement | Acceptance condition |
|---|---|---|
| F20 | Reconcile before adding | Search existing skill descriptions and relevant bodies before creating a skill |
| F21 | Consolidate continuously | Check local overlap after learning; queue broader consolidation at configured thresholds |
| F22 | Preserve complete skills | Merges validate support-file links, scripts, templates, trigger coverage, and exceptions |
| F23 | Distinguish usage from utility | Skill loading, invocation, modification, and observed successful outcomes are separate observations |
| F24 | Avoid arbitrary pruning | Low use alone cannot delete a skill; pinned and rare critical procedures are protected |
| F25 | Bound work | Enforce concurrency, model-call, token, runtime, and disk limits before starting work |
| F26 | Make operation inspectable | User can inspect reviews, evidence, pending work, Git diffs, eval results, costs, and application blockers |
| F27 | Recover after failures | Queue leases, retries, Git reconciliation, and crash tests prevent duplicate commits |
| F28 | Retain local control | Pause learning, disable automatic application, cancel queued experiments, or remove local evidence |

## 5. Automatic scope

Enrollment selects repository-relative paths. Initial suggestions are `CLAUDE.md`, `.claude/rules/`, `.claude/skills/`, `.claude/agents/`, project hook configuration, and explicitly named helper scripts. Tool configuration needs field-level validation: a change to a server description is different from adding an executable server.

The configured scope may include executable harness components. Before automatic application, their commands and dependency surfaces must match the enrolled execution policy and have relevant local tests. Adding a new executable, network destination, dependency source, or capability outside that policy produces `needs_scope_change`, not silent expansion. The initial enrollment is sufficient authorization for changes inside the defined scope; there is no routine approval prompt for each qualifying edit.

Excluded from automatic edits: credentials, global user settings, organization policy, application source unrelated to the harness, Git configuration/hooks, and Autotune's own decision machinery. Project files are treated as data during review and authoring; their instructions cannot authorize changing these exclusions.

## 6. Defaults to validate during implementation

These are proposed starting values, not measured performance claims.

| Setting | Initial value | Rationale |
|---|---|---|
| Review trigger | Every top-level response | Matches the intended workflow |
| Review concurrency | 1 | Bounds subscription pressure |
| Experiment concurrency | 1 per repository, 1 per machine by default | Prevents competing changes and burst usage |
| Candidate proposals | At most 1 per experiment | Keeps attribution and cost understandable |
| Review model | Configured low-cost Claude model | Short evidence review; verify availability at setup |
| Author and promotion judge | Configured capable Claude model | More demanding editing and comparison |
| Review input/output | At most 8,000 input tokens / 1,200 output tokens | Bounded evidence selection; record omissions |
| Experiment frequency | At most 2 started per day | All responses are reviewed; only selected hypotheses are tested |
| Experiment runtime | 20 minutes | Stop and report incomplete work rather than run indefinitely |
| Review calls | At most 200 per day | Excess episodes remain queued with visible budget status |
| Experiment model processes | At most 50 per day | Includes edits, baseline/candidate runs, and judges |
| Consolidation sweep | After 250 observed tool calls or 10 accepted skill edits | Initial scheduling heuristic; no automatic edit implied |
| Raw local evidence retention | 30 days | Minimize retained transcript material |
| Ordinary review/run detail | 180 days, configurable | Bound storage while retaining useful recent evidence |
| Applied/reverted decisions and active regression protections | Until project removal or explicit deletion | Preserve long-term lessons independently of raw trace retention |

Counters are scheduling policies, not efficacy thresholds. A required suite that does not fit the remaining budget stays pending; the worker must not weaken the gate to finish. Capture is unconditional while enabled; model review is eventual and subject to authentication, usage limits, and retention. Expired unreviewed evidence gets an explicit `evidence_expired` outcome.

## 7. Quality requirements

- **Foreground performance:** target p95 capture overhead below 150 ms and p99 below 500 ms on the reference machine; a 1-second enqueue timeout fails open with a visible diagnostic. Measure process startup and large-session behavior.
- **Data integrity:** one durable episode per stable identity; repeatable job decisions; no duplicate application after crash recovery. Model calls are at-least-once under uncertain failures and may incur duplicate usage.
- **Auditability:** any applied change is traceable to evidence, a hypothesis, exact baseline/candidate commits, the evaluator commit, actual model identifiers, and results.
- **Portability:** macOS and Linux tested first. Native Windows support is deferred until locking, process lifetime, and path behavior are tested.
- **Privacy:** local records by default; configured Claude calls receive the selected evidence. Redaction reduces exposure but is not guaranteed to identify every secret. No automatic publication of transcripts, database files, or eval reports.
- **Composability:** event observation preserves other mods' event/result values and does not automatically submit prompts to the user's conversation.

## 8. Success measures

Measure three different outcomes:

1. **Mechanical reliability:** capture completeness, duplicate handling, queue recovery, foreground overhead, and Git application integrity.
2. **Decision quality:** judge agreement with independently labeled cases, false acceptance of known regressions, appropriate abstention, and trace citation validity.
3. **User benefit:** task success on held-out tasks, recurrence of specific mistakes, retained known successes, total tokens/time including learning, useful skill retrieval, and revert frequency.

Judge score increases are diagnostic only. The release study must compare Autotune with a fixed harness, review-only operation, skill-only learning, and an equal-compute alternative. Proposed numerical release gates and the analysis method are in [Evaluation](EVALUATION.md).

## 9. Non-goals for v1

No weight training, general autonomous coding service, cross-user learning, transcript marketplace, hosted database, automatic pull requests in target projects, web dashboard, semantic embedding service, genetic search over many candidates, or custom harness versioning. No claim that the system can guarantee non-regression on unseen tasks.

## 10. Release definition

A first usable release must capture and review real top-level turns; survive retries and restarts; generate a scoped candidate through Claude Code; demonstrate a useful change and rejection of a seeded harmful change; consolidate two skills without losing covered behavior; and apply/revert via Git without modifying user work.

Automatic application remains disabled for an enrolled project until its evaluator, scope, and local execution policy pass setup checks. Report-only operation remains useful when a project has no adequate replay suite.
