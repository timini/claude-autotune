---
type: regex
pattern: '(PASS|FAIL)\s+python:'
match: contains
target: last_message
---
`autotune doctor` prints one line per check, starting with the Python check. The line can only come from the
worker CLI, so a match shows the command was registered, dispatched and answered by the mod.
