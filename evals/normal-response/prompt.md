---
schema_version: "1.1"
name: normal-response
description: A plain question gets the same short answer with the Autotune mod loaded as without it.
expected_outcome: The reply is the city name only, with no text from Autotune added to it.
tags: [passthrough, no-tools]
runs: 2
max_turns: 1
timeout_seconds: 120
---
What is the capital of France? Reply with only the city name and nothing else.
