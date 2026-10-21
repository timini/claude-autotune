"""Schema version 3 for new databases and in-place migration from versions 1 and 2."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autotune import store


def _by_table(cols) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for table, column, _ in cols:
        out.setdefault(table, set()).add(column)
    return out


_V2_COLUMNS = _by_table(store._V2_COLUMNS)
_V3_COLUMNS = _by_table(store._V3_COLUMNS)
_V2_INDEXES = ("reviews_signature", "candidates_signature", "candidates_status")


def old_schema_statements(version: int) -> list[str]:
    """An older schema: the current one without the columns (and, for
    version 1, the indexes) added after `version`."""
    later = [cols for v, cols in store._MIGRATIONS.items() if v > version]
    drop: dict[str, set[str]] = {}
    for cols in later:
        for table, names in _by_table(cols).items():
            drop.setdefault(table, set()).update(names)
    out = []
    for stmt in store._schema_statements():
        if version < 2 and any(f"INDEX IF NOT EXISTS {ix} " in stmt for ix in _V2_INDEXES):
            continue
        m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", stmt)
        names = drop.get(m.group(1), set()) if m else set()
        lines = [ln for ln in stmt.splitlines() if not (ln.startswith("    ") and ln.split()[0] in names)]
        out.append(re.sub(r",\s*\n\)", "\n)", "\n".join(lines)))
    return out


def v1_schema_statements() -> list[str]:
    return old_schema_statements(1)


def columns(conn, table) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autotune-migrate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "autotune.sqlite3"

    def make_v1(self):
        self.make_old(1)

    def make_old(self, version: int):
        raw = sqlite3.connect(self.db_path)
        for stmt in old_schema_statements(version):
            raw.execute(stmt)
        raw.execute(f"PRAGMA user_version={version}")
        raw.execute(
            "INSERT INTO episodes (episode_id, session_id, turn_id, lifecycle_id, lifecycle_source, origin, "
            "completed_at, captured_at, end_reason, repository_json, loaded_harness_json, coverage_json) "
            "VALUES ('ep1', 's', 't', 'l', 'startup', 'foreground', 'x', 'x', 'answer', '{}', '{}', '{}')"
        )
        raw.execute(
            "INSERT INTO reviews (review_id, episode_id, task_stage, review_json, recommendation, created_at) "
            "VALUES ('rv1', 'ep1', 'unknown', '{}', 'no_change', 'x')"
        )
        raw.commit()
        raw.close()

    def test_fixture_is_really_version_1(self):
        self.make_v1()
        raw = sqlite3.connect(self.db_path)
        try:
            for table, cols in {**_V2_COLUMNS, **_V3_COLUMNS}.items():
                self.assertFalse((_V2_COLUMNS.get(table, set()) | _V3_COLUMNS.get(table, set()))
                                 & columns(raw, table), table)
        finally:
            raw.close()

    def test_v1_database_is_migrated_in_place(self):
        self.make_v1()
        conn = store.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(store.get_meta(conn, "schema_version"), "3")
            for table, cols in [*_V2_COLUMNS.items(), *_V3_COLUMNS.items()]:
                self.assertLessEqual(cols, columns(conn, table), table)
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            self.assertLessEqual(set(_V2_INDEXES), indexes)
            row = conn.execute("SELECT * FROM reviews WHERE review_id = 'rv1'").fetchone()
            self.assertEqual(row["status"], "valid")
            self.assertEqual(row["adjustments_json"], "[]")
            self.assertEqual(row["input_evidence_json"], "[]")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE reviews SET status = 'bogus' WHERE review_id = 'rv1'")
        finally:
            conn.close()
        again = store.connect(self.db_path)
        try:
            self.assertEqual(again.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(again.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        finally:
            again.close()

    def test_new_database_has_the_same_columns_as_a_migrated_one(self):
        self.make_v1()
        migrated = store.connect(self.db_path)
        fresh = store.connect(self.tmp / "fresh.sqlite3")
        try:
            self.assertEqual(store.get_meta(fresh, "schema_version"), "3")
            for table in ("jobs", "reviews", "candidates", "model_calls", "evaluation_runs"):
                self.assertEqual(columns(migrated, table), columns(fresh, table), table)
        finally:
            migrated.close()
            fresh.close()

    def test_v2_database_with_a_candidate_is_migrated_to_v3(self):
        self.make_old(2)
        raw = sqlite3.connect(self.db_path)
        raw.execute(
            "INSERT INTO candidates (candidate_id, hypothesis, effect_metric, allowed_paths_json, change_kind, "
            "base_commit, status, reason, created_at, updated_at) "
            "VALUES ('cand1', 'h', 'm', '[]', 'instruction', 'abc', 'validating', NULL, 'x', 'x')"
        )
        raw.execute(
            "INSERT INTO evaluation_runs (run_id, candidate_id, case_id, arm, trial, attempt, harness_commit, "
            "started_at) VALUES ('run1', 'cand1', 'case-a', 'baseline', 1, 1, 'abc', 'x')"
        )
        raw.commit()
        self.assertFalse(_V3_COLUMNS["candidates"] & columns(raw, "candidates"))
        raw.close()

        conn = store.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
            for table, cols in _V3_COLUMNS.items():
                self.assertLessEqual(cols, columns(conn, table), table)
            cand = conn.execute("SELECT * FROM candidates WHERE candidate_id = 'cand1'").fetchone()
            self.assertEqual((cand["status"], cand["base_commit"]), ("validating", "abc"))
            self.assertIsNone(cand["gate_json"])
            self.assertIsNone(cand["target_branch"])
            conn.execute("UPDATE candidates SET gate_json = '{}', exposure = 'unknown', applied_at = 'y' "
                         "WHERE candidate_id = 'cand1'")
            run = conn.execute("SELECT * FROM evaluation_runs WHERE run_id = 'run1'").fetchone()
            self.assertIsNone(run["error_code"])
            self.assertIsNone(run["display_order"])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE candidates SET status = 'bogus' WHERE candidate_id = 'cand1'")
        finally:
            conn.close()

    def test_every_state_used_by_cycle_2_is_allowed(self):
        conn = store.connect(self.tmp / "fresh.sqlite3")
        try:
            for status in ("accepted", "waiting_to_apply", "stale", "applied", "observed_active",
                           "regression_suspected", "reverted", "needs_scope_change", "inconclusive"):
                conn.execute(
                    "INSERT INTO candidates (candidate_id, hypothesis, effect_metric, allowed_paths_json, "
                    "change_kind, base_commit, status, created_at, updated_at) "
                    "VALUES (?, 'h', 'm', '[]', 'instruction', 'abc', ?, 'x', 'x')",
                    (f"c-{status}", status),
                )
            for job_type in ("evaluate", "apply", "verify_active", "reproduce_regression"):
                conn.execute(
                    "INSERT INTO jobs (job_type, dedup_key, priority, state, next_eligible_at, created_at, "
                    "updated_at) VALUES (?, ?, 1, 'queued', 'x', 'x', 'x')",
                    (job_type, f"k:{job_type}"),
                )
        finally:
            conn.close()

    def test_unknown_newer_version_is_refused(self):
        raw = sqlite3.connect(self.db_path)
        raw.execute("PRAGMA user_version=9")
        raw.close()
        with self.assertRaises(RuntimeError):
            store.connect(self.db_path)


if __name__ == "__main__":
    unittest.main()
