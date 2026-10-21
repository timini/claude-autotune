---
schema_version: "1.1"
name: tool-passthrough
description: A tool call made with the Autotune mod loaded runs and returns its result unchanged.
expected_outcome: note.txt exists in the workspace, the Write tool was used once or more, and the reply is DONE.
tags: [passthrough, tools]
runs: 2
max_turns: 4
timeout_seconds: 180
allowed_tools: [Write]
---
Use the Write tool to create a file named note.txt in the current directory containing exactly the text `hello eval`.
When the file is written, reply with the single word DONE.
