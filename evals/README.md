# Plugin eval suite

This directory is the native `claude plugin eval` suite for the packaged Autotune mod. It checks the plugin as
distributed, not a project harness: the eval runner gives each run a temporary HOME and config directory, an empty
workspace and only the plugin under test, and it excludes project configuration. Project-harness comparisons use
Autotune's own replay runner instead (docs/EVALUATION.md section 2).

Status: written against the documented format for Claude Code 2.1.306 and not run yet. Every case calls a real
model, so nothing here runs in pull-request CI. `claude plugin validate .` and `claude plugin test .` do not run it.

## Cases

| Case | What it checks | Graders |
|---|---|---|
| `normal-response` | A plain question gets the same short answer with the mod loaded. | `Paris` in the last message; no `autotune` text in it |
| `tool-passthrough` | A tool call made with the mod loaded runs and its result reaches the model unchanged. | `note.txt` exists; Write used at least once; `DONE` in the last message |
| `command-surface` | `/autotune doctor` reaches the mod's command hook, which runs `python3 -m autotune doctor`. | a `PASS` or `FAIL` line for the Python check in the last message |

The workspace is empty and is not a Git repository, so the mod finds no `.autotune.json`, captures nothing and
starts no worker. These cases therefore show that loading the plugin leaves an unenrolled session alone. They do
not exercise reviews, candidates or replay.

By default the runner also runs a baseline arm without the plugin. `normal-response` and `tool-passthrough` should
pass in both arms; a difference between the arms is the signal. `command-surface` is expected to fail in the
baseline arm, because without the plugin there is no `/autotune` command.

## What the runner may not observe

`command-surface` assumes two things that the documentation does not settle and that have not been checked:

- that a prompt consisting of a slash command is expanded as a command, as it is when typed, rather than sent to
  the model as text;
- that the text the mod's `command.run` hook returns is what the `last_message` target reads.

If either is false, the case fails in both arms and says nothing about the mod. In that case drop it from the
suite and rely on `hooks/register.test.ts`, which `claude plugin test .` runs without a model, for the command
surface: it covers registration, dispatch and the returned text.

`tool-passthrough` sets `allowed_tools: [Write]`. The documentation names the key but not its value format; if the
runner rejects the list form, adjust it to what the runner reports.

## Running

Credentials are needed (the runner signs in as the configured user):

```sh
claude plugin eval . --no-publish
```

In CI, add `--trust-plugin`. `--ablation none` runs only the plugin arm. Results are written under
`evals/results/<timestamp>/`, which is ignored by Git. Do not publish results unless they have been checked to
contain nothing private; `--no-publish` keeps them local.

The protected workflow `.github/workflows/paid-tests.yml` runs this suite on manual dispatch only.

Cost: `normal-response` and `tool-passthrough` run twice per arm and `command-surface` once, so a default run with
the baseline arm is ten sessions, each limited to a few turns.
