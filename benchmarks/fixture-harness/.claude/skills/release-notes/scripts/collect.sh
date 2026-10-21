#!/bin/sh
# List merged changes since the last tag, one per line.
last=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
git log --merges --format='%s' "$last"..HEAD
echo "Collected $(git log --merges --format='%s' "$last"..HEAD | wc -l | tr -d ' ') changes"
