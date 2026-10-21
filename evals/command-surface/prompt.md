---
schema_version: "1.1"
name: command-surface
description: The /autotune command registered by the mod runs the worker CLI and returns its output.
expected_outcome: The doctor output from python3 -m autotune appears, with one PASS or FAIL line per check. The baseline arm without the plugin is expected to fail this case.
tags: [command, observability-unverified]
runs: 1
max_turns: 1
timeout_seconds: 120
---
/autotune doctor
