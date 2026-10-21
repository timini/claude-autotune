---
name: release-notes
description: Draft release notes from merged changes. Use when the user asks for release notes or a changelog entry.
---
# Release notes

1. Run [the collector](scripts/collect.sh) to list merged changes since the last tag.
2. Print the collector summary line "Collected N changes" before the notes.
3. Group entries under Added, Changed and Fixed.

Never include internal ticket ids such as INT-1234 in public release notes.
