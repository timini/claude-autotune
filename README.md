# Continua

**A proposed Claude Code mod that reviews every finished response, tests improvements to the project harness, and consolidates skills over time.**

Status: research and implementation specification, dated **21 October 2026**. This repository contains documentation and synthetic examples. The plugin is not implemented yet.

Continua combines retrospective harness improvement, persistent evaluation evidence, and continuous skill consolidation. It uses Claude Code for reviews, judging, and edits. **Git handles change history and rollback. SQLite records reviews, experiments, outcomes, and the Git commits involved.**

## Intended behavior

1. At each completed response, capture an evidence record and queue a background review.
2. Record what worked, what failed, and what remains unknown. Give ratings with evidence references.
3. Propose a small harness change when the evidence supports one. Doing nothing is a valid result.
4. Use a separate Claude Code process to edit instructions, skills, hooks, or tool configuration in a disposable Git worktree.
5. Compare the current harness and candidate on the same tasks, including known successes that must be preserved.
6. Apply qualifying changes through Git when the project checkout is ready. Keep unsuccessful experiments in the evidence database.
7. Consolidate related skills through the same tests and promotion rules.

Reviews happen on **every finished response**, including a pause within a longer session. Expensive experiments are queued and bounded; a response does not imply a new edit.

## Documentation

| Document | Purpose |
|---|---|
| [Product requirements](docs/PRD.md) | Scope, user behavior, requirements, defaults, acceptance criteria |
| [Architecture](docs/ARCHITECTURE.md) | Native mod, worker, SQLite, Git, concurrency, model processes |
| [Evaluation and test strategy](docs/EVALUATION.md) | Mechanics, judge calibration, paired replay, regression gates, experiments |
| [Implementation plan](docs/IMPLEMENTATION_PLAN.md) | Ordered work packages, dependencies, deliverables, exit criteria |
| [Prior art and research](docs/RESEARCH.md) | What already exists, primary sources, limitations, design implications |
| [Data and process contracts](docs/CONTRACTS.md) | Records, states, examples, validation, proposed commands |
| [Decisions](docs/DECISIONS.md) | Settled product choices and remaining implementation checks |

## What already exists?

[RHO](https://github.com/wbopan/retro-harness) is the closest precedent for retrospective harness optimization. [Tigerless AutoHarness](https://github.com/tigerless-labs/autoharness) provides direct inspiration for continual skill extraction and consolidation. [Hermitd](https://github.com/gtapps/hermitd) separates reflection from evidence judging, and [Kayba AutoHarness](https://github.com/kayba-ai/autoharness) connects harness changes to benchmark results. Continua's proposed contribution is their integration into a bounded, per-response workflow with a persistent record of attempted changes and observed regressions. This is a product design, not a claim that self-improving harnesses are new.

## Deliberate constraints

- Claude Code first, using separate CLI processes and the user's configured subscription. Other provider adapters can follow.
- Whole **project** harness scope, with explicit editable paths; no automatic changes to global account settings or Continua's own judge and controller.
- Local replays in disposable worktrees. **No Docker dependency.** Worktrees are not a security boundary: approved hooks and scripts run with the local user's access.
- One experiment at a time; cheap reviews at every pause; heavier validation can wait.
- No custom harness versioning, vector database, hosted service, or dashboard in v1.
- No promise of zero regressions. Promotion gates reduce known risks; held-out tests and observed failures measure what those gates miss.

All proposed commands, configuration fields, and example records are specifications, not working interfaces. No model evaluation or effectiveness result is claimed by this repository.
