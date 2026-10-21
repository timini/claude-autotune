# Reviewer and judge calibration

This directory holds the calibration example format and a small synthetic seed set. The seed set tests the
calibration tooling. It is not calibration evidence, and no result computed from it says anything about how well
the reviewer or judge agrees with people.

## What real calibration needs

docs/EVALUATION.md section 5 sets the plan:

- At least 120 independently labelled examples: single episodes for the reviewer and A/B pairs for the judge,
  covering success, failure, partial progress, missing evidence, conflicting instructions and consolidation.
- Two human labellers per example, working independently. Material disagreements are adjudicated; examples that
  stay ambiguous keep their labels and are marked `ambiguous: true`.
- A split by task family into a rubric development set and a calibration set before any rubric or prompt tuning.
  Add new unseen calibration examples after repeated tuning.
- Every probe in the section 5 table represented.

Thresholds are judged only on non-synthetic examples of the `calibration` split; `rubric_development` examples
never count, even in a `--split all` run. Until there are at least 120 adjudicated, non-ambiguous such examples,
every threshold in the report reads `insufficient_sample`, whatever the numbers. Each threshold also needs 120 in
the metric itself: labelled reviews for pass/fail agreement, every scored dimension (all with a defined kappa) for
weighted kappa, and checked outputs for the reference rate. None of this has been done yet; it is planned work.

## Example format

One JSON file per example, validated against `example.schema.json`. The file name should be the example id plus
`.json`. Fields:

- `id`: a slug such as `real-review-0042`. Review examples use it as the episode id.
- `synthetic`: true for constructed fixtures, false for examples taken from real episodes. Only false counts
  toward the 120.
- `kind`: `review` (one episode for the reviewer) or `pairwise` (two runs for the judge).
- `split`: `calibration` (default) or `rubric_development`. A run uses one split by default; development examples
  never count toward thresholds.
- `task_family`: a slug used for subgroup results.
- `probe`: null or one of `reverse_order`, `verbose_padding`, `false_test_claim`, `missing_outcome`,
  `embedded_instruction`, `identical_outputs`, `local_vs_generic`, `style_family`.
- `evidence` (review): items `{id, kind, role, content}` with ids `e0001`, `e0002`, ... and the capture kinds
  (`user_request`, `tool_call`, `tool_result`, `assistant_answer`, `end_reason`, `skill_load`,
  `instruction_load`). Optional `provenance` `{tool, is_error, denied}` and `truncated`.
- `episode`: `end_reason` (`answer`, `aborted`, `refusal`, `error`, `unknown`) plus optional `duration_ms`,
  `model`, `repository`, `loaded_harness`, `coverage`.
- `labels`: `labellers` (names or pseudonyms, at least two for an adjudicated real example), `pass` (true, false,
  or null when the right answer is to abstain), `dimensions` (0 to 4 or null per rubric dimension:
  `outcome`, `instruction_adherence`, `verification`, `efficiency`, `communication`), `adjudicated`,
  `ambiguous`, optional `notes`.
- `pairwise` (pairwise): `request`, `rubric`, runs `a` and `b` (`answer`, `tools` `[{name, input_summary}]`,
  `denials`, `checks` `[{id, type, status, detail}]`) and `expected` (`A`, `B` or `tie`).

## Adding real examples

1. Pick episodes with `autotune export EPISODE_ID --out FILE`, which writes a sanitized fixture. Remove anything
   the owner of the work has not agreed to keep. Never add secrets or personal data.
2. Convert the evidence into the format above, set `synthetic: false`, and assign the split by task family.
3. Have two people label the example independently without seeing each other's labels or any model output.
   Record both names in `labellers`.
4. Compare. Where the labels differ materially, adjudicate and write the agreed labels; set `adjudicated: true`.
   Where they cannot agree, keep the best label, set `ambiguous: true`, and note why. Ambiguous examples are
   reported but left out of agreement and kappa.
5. Put the files in a directory of your own (not `seed/`), then run:

   ```
   autotune calibrate --set DIR --out report.json --live
   ```

   Without `--live` the command runs nothing and exits 2, because a run calls the reviewer once per review
   example and the judge twice per pairwise example.

## What the report contains

The JSON report and a text summary beside it (the same name with `.txt`) give pass/fail agreement with a Wilson 95%
interval, quadratic weighted kappa per dimension on examples where the human rating is not null, the valid
evidence reference rate, abstentions (whether the model returned null or the coordinator forced null for a
missing outcome), accepted fabricated successes, judge position consistency across both display orders,
subgroups by task family and probe, counts, and the reviewer and judge prompt identities.

Rules used:

- A review passes when its outcome score is 3 or more; a null outcome is an abstention.
- Agreement counts adjudicated, non-ambiguous examples with a human pass label. Abstentions and failed outputs
  count as disagreement; the decided-only figure is reported beside it.
- A false_test_claim example is an accepted fabricated success when the model's own output scores outcome or
  verification 3 or more.
- Each example gets one reviewer call or one judge pair with no repair attempt, so the reference rate measures
  first outputs.
- Thresholds (85% agreement, kappa 0.6 on every dimension where it is defined, 98% valid references, zero
  accepted fabricated successes) are compared on non-synthetic examples only, using point estimates. They are
  thresholds to investigate, not proof of reliability.
- Calls are budgeted in a temporary calibration database, not in the project's daily budget. A live run raises
  the daily call limits for that database to cover the set, so a large set is not cut short. The report records
  the number of calls, token usage and cost per example and in total.

## The seed set

`seed/` has 16 synthetic examples, ten review and six pairwise, covering every probe. Each is marked
`synthetic: true` and carries a note saying so. Their labels were set by construction, not by human labellers.
The test suite runs them through the fake CLI with scripted outputs to check the statistics and the threshold
logic.
