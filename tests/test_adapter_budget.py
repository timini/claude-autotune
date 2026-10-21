from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import timedelta

from autotune import budget, queue
from tests._adapter_support import REPO_ROOT, T0, AdapterTestCase, make_cfg, open_db


class BudgetTests(AdapterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = open_db(self.db_path)
        self.addCleanup(self.conn.close)

    def _reserve(self, role, now=T0, cfg=None):
        return budget.reserve(
            self.conn, role, cfg or make_cfg(review_calls_per_day=2, experiment_calls_per_day=3), now,
            job_id=None, attempt=1, requested_model=None, limits={"timeout_seconds": 5},
        )

    def test_reserve_inserts_a_reserved_row(self):
        call_id = self._reserve("review")
        row = self.conn.execute("SELECT * FROM model_calls WHERE call_id = ?", (call_id,)).fetchone()
        self.assertEqual(row["outcome"], "reserved")
        self.assertEqual(row["role"], "review")
        self.assertIsNone(row["finished_at"])
        self.assertEqual(json.loads(row["limits_json"]), {"timeout_seconds": 5})

    def test_review_limit_counts_every_outcome_including_errors(self):
        first = self._reserve("review")
        budget.settle(self.conn, first, T0, "error", error_code="timeout", usage_uncertain=True)
        self._reserve("review")
        with self.assertRaises(budget.BudgetExhausted) as caught:
            self._reserve("review")
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual(queue.classify_error(caught.exception), "budget_exhausted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 2)

    def test_groups_are_independent(self):
        self._reserve("review")
        self._reserve("review")
        for role in ("author", "judge", "replay"):
            self._reserve(role)
        with self.assertRaises(budget.BudgetExhausted):
            self._reserve("curator")
        with self.assertRaises(budget.BudgetExhausted):
            self._reserve("review")

    def test_new_utc_day_resets_the_count(self):
        late = T0.replace(hour=23, minute=59, second=59)
        self._reserve("review", now=late)
        self._reserve("review", now=late)
        with self.assertRaises(budget.BudgetExhausted):
            self._reserve("review", now=late)
        self._reserve("review", now=late + timedelta(seconds=2))

    def test_settle_records_usage_model_cost_and_session(self):
        call_id = self._reserve("author")
        budget.settle(
            self.conn, call_id, T0 + timedelta(seconds=3), "ok", "claude-x", {"output_tokens": 7},
            0.25, 3000, None, "sess-1", False,
        )
        row = self.conn.execute("SELECT * FROM model_calls WHERE call_id = ?", (call_id,)).fetchone()
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["resolved_model"], "claude-x")
        self.assertEqual(json.loads(row["usage_json"]), {"output_tokens": 7})
        self.assertEqual(row["cost_usd"], 0.25)
        self.assertEqual(row["duration_ms"], 3000)
        self.assertEqual(row["cli_session_id"], "sess-1")
        self.assertEqual(row["usage_uncertain"], 0)
        self.assertIsNotNone(row["finished_at"])

    def test_settle_rejects_unknown_outcome(self):
        call_id = self._reserve("review")
        with self.assertRaises(ValueError):
            budget.settle(self.conn, call_id, T0, "reserved")

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            self._reserve("planner")

    def test_usage_today_reports_limits_and_unsettled(self):
        cfg = make_cfg(review_calls_per_day=5, experiment_calls_per_day=3)
        old = self._reserve("review", now=T0 - timedelta(hours=2), cfg=cfg)
        self._reserve("review", now=T0, cfg=cfg)
        done = self._reserve("author", now=T0, cfg=cfg)
        budget.settle(self.conn, done, T0, "ok")
        self._reserve("review", now=T0 - timedelta(days=1), cfg=cfg)
        usage = budget.usage_today(self.conn, T0, cfg)
        self.assertEqual(usage["day"], "2026-03-04")
        self.assertEqual(usage["roles"]["review"], 2)
        self.assertEqual(usage["roles"]["author"], 1)
        self.assertEqual(usage["groups"]["review"]["used"], 2)
        self.assertEqual(usage["groups"]["review"]["limit"], 5)
        self.assertEqual(usage["groups"]["review"]["remaining"], 3)
        self.assertEqual(usage["groups"]["review"]["unsettled"], 1)
        self.assertEqual(usage["groups"]["review"]["in_flight"], 1)
        self.assertEqual(usage["groups"]["experiment"]["remaining"], 2)
        self.assertEqual(usage["unsettled"], 2)
        budget.settle(self.conn, old, T0, "error")
        self.assertEqual(budget.usage_today(self.conn, T0)["groups"]["review"]["unsettled"], 0)


RACE_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from datetime import datetime, timezone
    sys.path.insert(0, {root!r})
    from tests._adapter_support import open_db, make_cfg
    from autotune import budget
    start_at = float(sys.argv[2])
    conn = open_db(sys.argv[1])
    while time.time() < start_at:
        time.sleep(0.001)
    try:
        budget.reserve(conn, "review", make_cfg(review_calls_per_day=3),
                       datetime(2026, 3, 4, 12, tzinfo=timezone.utc),
                       job_id=None, attempt=1, requested_model=None, limits={{}})
        print("reserved")
    except budget.BudgetExhausted:
        print("exhausted")
    """
)


class BudgetRaceTests(AdapterTestCase):
    def test_processes_racing_for_the_last_slot_take_it_once(self):
        conn = open_db(self.db_path)
        cfg = make_cfg(review_calls_per_day=3)
        for _ in range(2):
            budget.reserve(conn, "review", cfg, T0, job_id=None, attempt=1, requested_model=None, limits={})
        conn.close()
        import time

        start_at = time.time() + 1.0
        script = RACE_SCRIPT.format(root=str(REPO_ROOT))
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.db_path), str(start_at)],
                cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(6)
        ]
        outputs = []
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
            outputs.append(out.strip())
        self.assertEqual(outputs.count("reserved"), 1, outputs)
        self.assertEqual(outputs.count("exhausted"), 5, outputs)
        self.assertEqual(len(self.model_calls()), 3)
