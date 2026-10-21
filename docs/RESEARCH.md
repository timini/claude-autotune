# Prior art and research

Research date: **21 October 2026**. Scope: primary papers, official Claude Code documentation, public repository documentation, and selected source files. External projects were inspected statically; their benchmark claims were not reproduced.

## 1. Has this been done before?

**The major parts have.** Retrospective harness optimization, automatic skill accumulation/consolidation, evidence judging, and benchmark-gated harness edits all have direct precedents. The useful product question is whether these parts can work together reliably at every response boundary without excessive cost or degrading earlier behavior.

Autotune is a proposed integration: review each completed response, retain evidence of ratings and attempted changes, consolidate skills, test a bounded candidate, and apply it using Git. This research did not establish that no existing project implements the exact combination; it does not support a first-of-its-kind claim.

## 2. Closest implementations

| Project | Observed mechanism | Useful inspiration | Important difference from Autotune |
|---|---|---|---|
| RHO / retro-harness | Mine previous trajectories, diagnose, propose, probe, compare, apply | Retrospective failure analysis and candidate replay | Broad retrospective cycles; Autotune needs bounded per-response ingress and persistent operational evidence |
| Tigerless AutoHarness | Background reflection, learned skills, periodic curation, deterministic promotion | Continuous consolidation and complete skill-package handling | Focuses on agent-created skills; Autotune can edit enrolled existing project harness files |
| Hermitd | Reflection candidates with a separate evidence-checking judge | Verify citations and separate proposal from validation | Broader assistant platform and approval workflow; Autotune is a small project mod with tested automatic application |
| Kayba AutoHarness | Change an existing harness, run a benchmark command, retain useful candidates | Outcome-oriented benchmark adapter | Campaign/control-plane design; Autotune emphasizes pauses within ordinary interactive work |

### RHO / retro-harness

The [RHO paper](https://arxiv.org/abs/2606.05922v3), submitted June 2026 and revised August 2026, describes self-supervised retrospective harness optimization. It selects challenging past tasks, generates new attempts, and uses pairwise self-preference to select harness changes. The authors report a SWE-Bench Pro increase from 59% to 78% in their setting. That is an author-reported benchmark result, not evidence that Autotune will achieve the same gain.

The inspected [Claude retrospection workflow](https://github.com/wbopan/retro-harness/blob/e5f2d1a8a06ab3523ab42e0042d2fa13d9acb701/.claude/workflows/retrospection.js) edits instructions, memory, and helpers. It selects past sessions, uses worktrees for probes, and judges candidates. Its defaults include multiple diagnoses and candidates; it avoids very recent sessions. The same configured model is used across roles. When probes are unavailable, the workflow can compare harness text directly.

**Design implication:** borrow the diagnosis, edit and replay structure. Use fresh baseline and candidate runs from an identical task fixture, because past and current project states may differ. Keep one candidate at a time. A text-only preference without outcome evidence must not authorize automatic application. These are proposed changes to the approach, not claims of defects established by execution.

The separate [Python optimization loop](https://github.com/wbopan/retro-harness/blob/e5f2d1a8a06ab3523ab42e0042d2fa13d9acb701/src/rho/loop.py) is useful for understanding reusable optimization boundaries. Autotune does not need to adopt RHO's storage/control structures; Git already supplies the desired file history.

### Tigerless AutoHarness: the 🐯 reference

The [inspected repository](https://github.com/tigerless-labs/autoharness/tree/27d13c1d8091681db05a10ef9626081217da240f) provides a close match for constant skill learning. Its documented defaults schedule reflection after 50 tool calls and consolidation after 250, rather than reviewing every response. It distinguishes skill events and uses a separate deterministic writer to validate proposed changes.

The [curator instructions](https://github.com/tigerless-labs/autoharness/blob/27d13c1d8091681db05a10ef9626081217da240f/agents/curator.md) deliberately combine narrow skills into broader, discoverable skills. They require carrying references, templates, scripts, and assets with the merge. They also reject zero usage as sufficient evidence for curator pruning and limit edits to agent-created skills.

**Design implication:** integrate consolidation into the normal improvement loop. Keep specific knowledge in sections and support files; validate the union of original behaviors after a merge. Use usage as a scheduling hint, not a correctness label. Autotune deliberately broadens the editable scope to enrolled existing project skills and uses Git instead of a second archive of previous skill bodies.

The [reflector instructions](https://github.com/tigerless-labs/autoharness/blob/27d13c1d8091681db05a10ef9626081217da240f/agents/reflector.md) are additional inspiration for checking existing skills before creating more. The lesson is to update the right existing procedure, not create one new skill for every turn.

### Hermitd

The [Hermitd repository](https://github.com/gtapps/hermitd/tree/f2f8e111f3079fe19d619d13ceadc73a3a5bf1e4) combines reflection with a broader persistent assistant workflow. Its [reflection judge](https://github.com/gtapps/hermitd/blob/f2f8e111f3079fe19d619d13ceadc73a3a5bf1e4/plugins/hermitd/agents/reflection-judge.md) checks cited evidence in normalized task records and emits accept, downgrade, or suppress decisions. Missing records and capped evidence have explicit handling. Evidence origin and source are distinguished.

**Design implication:** require a reference that resolves to actual captured evidence; do not let a proposal certify itself. Autotune can keep this separation without adopting Hermitd's larger operating structure. Its configured automatic application should rely on replay gates, rather than a mandatory manual approval for every ordinary change.

### Kayba AutoHarness

The [Kayba repository](https://github.com/kayba-ai/autoharness/tree/7cfd52e12fe1d72a3e321da91a56433edfcb37c5) describes a harness optimizer driven by a target root and benchmark command. It supports prompt, configuration, middleware, and source changes, and keeps or discards candidates based on evaluation. It integrates with Claude Code and other coding tools.

**Design implication:** make the evaluator an explicit adapter with a stable result contract. Do not infer improvement from a plausible edit. Autotune needs a smaller per-project loop, not the whole campaign management model.

## 3. Research that changes the design

### ACE: preserve useful detail during consolidation

[Agentic Context Engineering](https://arxiv.org/abs/2510.04618v3) uses generation, reflection, and curation to maintain useful context. It describes loss of detail from repeated broad rewriting and advocates structured incremental changes.

For Autotune, this supports small changes and a coverage map for merges. A shorter instruction file is not inherently better. Evaluate whether the agent still retrieves the needed procedure, including exceptions, and measure loaded context separately from correctness.

### GEPA: natural-language feedback can guide search

[GEPA](https://arxiv.org/abs/2507.19457v2) uses reflective feedback to propose prompt improvements and retain complementary candidates. It supports the usefulness of diagnoses richer than a single numeric reward.

Autotune should store the reason for failure and the expected behavioral effect. Its balanced v1 does not need genetic search or a population of candidate harnesses. A single supported candidate is easier to attribute and budget.

### Harness evolution requires fair evaluation

[Rethinking the Evaluation of Harness Evolution for Agents](https://arxiv.org/abs/2607.12227v4), revised 5 October 2026, reports that harness evolution on its coding benchmark did not outperform simpler task-level scaling methods under comparable budgets and showed limited generalization. It found more promising results in long-horizon games.

This does not refute all harness improvement. It requires Autotune to compare against extra task attempts at matched compute and to use genuinely held-out tasks. Repeatedly tuning on the regression bank is optimization; its final score cannot also establish generalization.

### A separate judge is still biased

[Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685) studies position, verbosity, and self-enhancement biases. [A separate position-bias study](https://arxiv.org/abs/2406.07791) examines how answer order changes pairwise judgment.

Autotune therefore needs blind arm labels, both answer orders, calibrated abstention, independent outcome checks, and a human-labeled calibration set. Separate Claude processes solve context separation, not model-family bias. Higher average ratings alone cannot establish better performance.

### Agent outcomes and infrastructure need explicit controls

[Anthropic's agent evaluation guidance](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) distinguishes different graders and emphasizes actual task outcomes and repeated trials. Its [infrastructure study](https://www.anthropic.com/research/infrastructure-noise) demonstrates substantial benchmark sensitivity to execution conditions.

For Autotune, preserve both a capability suite and known-success cases, record actual artifacts, and pin resource limits and dependencies. A broken environment is neither a failed harness nor a passing candidate.

## 4. Claude Code feasibility

Official documentation and local CLI help were checked for these implementation decisions. The local CLI was **2.1.306**. Documentation support is not the same as an end-to-end tested integration.

| Capability | Evidence | Consequence |
|---|---|---|
| Native mod lifecycle and skill observation | [Mods overview](https://code.claude.com/docs/en/plugins/mods/overview), [event reference](https://code.claude.com/docs/en/plugins/mods/reference) | Native capture rather than a transcript-polling service |
| Middleware observation | [Event handling](https://code.claude.com/docs/en/plugins/mods/events) | Preserve other mods' results and foreground behavior |
| Session/process APIs | [Mods API](https://code.claude.com/docs/en/plugins/mods/api) | Short durable ingress; detached worker does expensive work |
| Mocked native tests | [Mod testing](https://code.claude.com/docs/en/plugins/mods/test) | Test event behavior without paid model calls |
| Packaged plugin evals | [Plugin evals](https://code.claude.com/docs/en/plugin-evals) | Useful for distribution; a distinct full-project runner is still necessary |
| Programmatic CLI output | [Headless use](https://code.claude.com/docs/en/headless), local `claude --help` | Separate processes with typed outputs and explicit limits |
| Subscription auth | [Authentication](https://code.claude.com/docs/en/authentication) | Supported sign-in/token flow; no extracted credentials |
| Skill/instruction loading | [Skills](https://code.claude.com/docs/en/skills), [memory](https://code.claude.com/docs/en/memory) | Track effective exposure separately from a committed edit |
| Local concurrency/history | [SQLite WAL](https://www.sqlite.org/wal.html), [Git worktrees](https://git-scm.com/docs/git-worktree.html) | Local evidence DB and ordinary Git; worktrees are not sandboxes |

The most consequential limitation is the native plugin eval runner's omission of project customizations. Testing a bundled plugin in that environment cannot demonstrate that a changed project `CLAUDE.md`, hook, skill, or MCP setup works in the real project. Autotune's replay runner must load the intended complete harness explicitly.

The user selected local replay over Docker. Executable tests therefore run with local account privileges. Controlled commands and disposable worktrees address accidental interference; they cannot support claims of adversarial containment.

## 5. Recommended synthesis

Use RHO's retrospective optimization structure, Tigerless's consolidation discipline, Hermitd's citation verification, and Kayba's explicit outcome evaluator. Add a durable per-response queue, ordinary Git application/revert, and a deliberately small control loop.

The product-specific work is not inventing another optimizer. It is making evidence trustworthy, experiments comparable, updates bounded, and ongoing skill consolidation measurable. The [architecture](ARCHITECTURE.md) and [evaluation plan](EVALUATION.md) specify how to test those claims.

## 6. Evidence inventory and limitations

| Source | Inspected reference | Evidence level |
|---|---|---|
| RHO repository | `e5f2d1a8a06ab3523ab42e0042d2fa13d9acb701` | README, Claude workflow, Python loop |
| Tigerless AutoHarness | `27d13c1d8091681db05a10ef9626081217da240f` | README, reflector/curator, selected hooks, promotion and lifecycle code |
| Hermitd | `f2f8e111f3079fe19d619d13ceadc73a3a5bf1e4` | README, reflection skill and judge |
| Kayba AutoHarness | `7cfd52e12fe1d72a3e321da91a56433edfcb37c5` | README and documented integration |
| Research papers | Versions linked above | Methods/claims reviewed; no reproduction |
| Claude Code | Docs accessed 21 October 2026; local CLI 2.1.306 | Documentation and help checked; proposed mod not run |

Repository SHAs identify the code reviewed, not a versioning mechanism to build into Autotune. Source licenses and notices must be checked before copying implementation code; this design cites prior work without vendoring it. Search coverage is substantial but not exhaustive. Marketing claims are not treated as validation, and all Autotune defaults and thresholds remain hypotheses until tested.
