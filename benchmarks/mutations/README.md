# Gate mutation set

Deliberate harness changes against the synthetic project in `../fixture-harness/`, used to test the promotion gate
(docs/EVALUATION.md section 8). Run with `python3 -m autotune.mutations` (simulated) or through `/autotune mutations`.

- 40 harmful mutations, 4 in each category: removed_verification, broadened_trigger, lost_exception, wrong_path,
  broken_reference, suppressed_error, fabricated_success, weakened_tool_restriction, changed_grader, new_executable.
- 9 useful and 6 neutral mutations, so a gate that rejects everything cannot pass. useful-09, neutral-05 and
  neutral-06 are consolidations that pass skills.validate_merge and reach the gate's consolidation rule: a
  correct merge that must be accepted, one that saves under 10% of the loaded skill tokens, and one whose
  outcomes differ between arms.
- `_baseline.json` holds the simulated outcome of every case on the unchanged harness. A mutation's
  `simulated_runs` overrides it per case and arm.

Each harmful mutation also improves its target cases in the simulated runs, so the harm has to be found by the
check named in `detected_by` (a static violation, a critical regression or a known-success regression). The
run fails unless every mutation is caught by a check it names, so a disabled check cannot hide behind
another one.
changed_grader and new_executable mutations have no runs: the static checks must catch them.

Simulated runs are hand-written, plausible stand-ins for model replays. A passing simulated run shows the gate
logic holds on them. It is not evidence of how a model behaves; live mode (`--live --budget-calls N`) runs the
real evaluate path and is paid.
