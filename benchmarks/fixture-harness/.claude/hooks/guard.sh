#!/bin/sh
# Block Bash commands that delete files under data/.
input=$(cat)
case "$input" in
  *"rm "*data/*) echo "guard: deleting files under data/ is not allowed" >&2; exit 2 ;;
esac
exit 0
