---
name: db-migrate
description: Plan and check database schema migrations. Use when the user asks to add, review or run a migration.
---
# Database migrations

1. Run `scripts/check_migrations.sh` to list pending migrations in `db/migrations/`.
2. Explain each pending migration before running anything.

Do not run migrations against production unless the user names the change ticket.
