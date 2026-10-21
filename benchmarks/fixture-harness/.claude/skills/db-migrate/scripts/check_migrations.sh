#!/bin/sh
# List migration files that are not recorded in db/applied.txt.
for f in db/migrations/*.sql; do
  name=$(basename "$f")
  grep -qx "$name" db/applied.txt 2>/dev/null || echo "pending: $name"
done
