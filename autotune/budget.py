"""Daily model call budgets backed by the model_calls table.

A call is reserved before the process starts and settled after it ends. The
count and the insert share one BEGIN IMMEDIATE transaction, so two workers
cannot both take the last slot of a day.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import store
from .util import canonical_json, iso, new_id

GROUPS = {
    "review": ("review",),
    "experiment": ("author", "judge", "curator", "replay"),
}
GROUP_LIMIT_KEYS = {
    "review": "review_calls_per_day",
    "experiment": "experiment_calls_per_day",
}
UNSETTLED_AFTER = timedelta(hours=1)
SETTLE_OUTCOMES = ("ok", "error", "cancelled")


class BudgetExhausted(Exception):
    code = "budget_exhausted"

    def __init__(self, group: str, used: int, limit: int) -> None:
        super().__init__(f"{group} budget exhausted: {used} of {limit} calls used today")
        self.group = group
        self.used = used
        self.limit = limit


def group_for(role: str) -> str:
    for group, roles in GROUPS.items():
        if role in roles:
            return group
    raise ValueError(f"unknown model call role {role!r}")


def limit_for(group: str, cfg: dict) -> int:
    return int(cfg[GROUP_LIMIT_KEYS[group]])


def day_bounds(now: datetime) -> tuple[str, str]:
    """UTC calendar day containing now, as ISO strings [start, end)."""
    utc = now.astimezone(timezone.utc)
    start = datetime(utc.year, utc.month, utc.day, tzinfo=timezone.utc)
    return iso(start), iso(start + timedelta(days=1))


def _count(conn: sqlite3.Connection, roles: tuple[str, ...], start: str, end: str) -> int:
    marks = ", ".join("?" for _ in roles)
    row = conn.execute(
        f"SELECT COUNT(*) FROM model_calls WHERE role IN ({marks}) "
        "AND started_at >= ? AND started_at < ?",
        (*roles, start, end),
    ).fetchone()
    return int(row[0])


def reserve(
    conn: sqlite3.Connection,
    role: str,
    cfg: dict,
    now: datetime,
    *,
    job_id: int | None,
    attempt: int,
    requested_model: str | None,
    limits: dict | None = None,
) -> str:
    """Insert a 'reserved' model_calls row and return its call_id, or raise
    BudgetExhausted when the role's group has no calls left today."""
    group = group_for(role)
    limit = limit_for(group, cfg)
    start, end = day_bounds(now)
    call_id = new_id("call")
    with store.transaction(conn):
        used = _count(conn, GROUPS[group], start, end)
        if used >= limit:
            raise BudgetExhausted(group, used, limit)
        conn.execute(
            "INSERT INTO model_calls (call_id, job_id, attempt, role, requested_model, "
            "limits_json, started_at, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved')",
            (call_id, job_id, attempt, role, requested_model, canonical_json(limits or {}), iso(now)),
        )
    return call_id


def settle(
    conn: sqlite3.Connection,
    call_id: str,
    now: datetime,
    outcome: str,
    resolved_model: str | None = None,
    usage: dict | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    cli_session_id: str | None = None,
    usage_uncertain: bool = False,
) -> None:
    if outcome not in SETTLE_OUTCOMES:
        raise ValueError(f"unknown settle outcome {outcome!r}")
    with store.transaction(conn):
        conn.execute(
            "UPDATE model_calls SET finished_at = ?, outcome = ?, resolved_model = ?, usage_json = ?, "
            "cost_usd = ?, duration_ms = ?, error_code = ?, cli_session_id = ?, usage_uncertain = ? "
            "WHERE call_id = ?",
            (
                iso(now),
                outcome,
                resolved_model,
                canonical_json(usage) if usage is not None else None,
                cost_usd,
                duration_ms,
                error_code,
                cli_session_id,
                int(bool(usage_uncertain)),
                call_id,
            ),
        )


def usage_today(conn: sqlite3.Connection, now: datetime, cfg: dict | None = None) -> dict:
    """Calls used today per role and per group.

    `unsettled` counts reservations over an hour old that never finished: calls
    whose real cost is unknown. `in_flight` counts younger open reservations.
    Group limits are included when cfg is given.
    """
    start, end = day_bounds(now)
    stale = iso(now - UNSETTLED_AFTER)
    roles = {role: 0 for roles in GROUPS.values() for role in roles}
    for row in conn.execute(
        "SELECT role, COUNT(*) FROM model_calls WHERE started_at >= ? AND started_at < ? GROUP BY role",
        (start, end),
    ):
        roles[row[0]] = int(row[1])
    groups: dict[str, dict] = {}
    for group, members in GROUPS.items():
        marks = ", ".join("?" for _ in members)
        open_rows = conn.execute(
            f"SELECT started_at FROM model_calls WHERE role IN ({marks}) "
            "AND finished_at IS NULL AND started_at >= ? AND started_at < ?",
            (*members, start, end),
        ).fetchall()
        unsettled = sum(1 for r in open_rows if r[0] < stale)
        entry = {
            "used": sum(roles[r] for r in members),
            "unsettled": unsettled,
            "in_flight": len(open_rows) - unsettled,
        }
        if cfg is not None:
            entry["limit"] = limit_for(group, cfg)
            entry["remaining"] = max(0, entry["limit"] - entry["used"])
        groups[group] = entry
    total_unsettled = conn.execute(
        "SELECT COUNT(*) FROM model_calls WHERE finished_at IS NULL AND started_at < ?", (stale,)
    ).fetchone()[0]
    return {"day": start[:10], "roles": roles, "groups": groups, "unsettled": int(total_unsettled)}
