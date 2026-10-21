"""10,000 synthetic turn_complete events through ingress.main."""

from __future__ import annotations

import io
import json
import os
import random
import sys
import time
from unittest import mock

from autotune import ingress
from tests._integration_support import IntegrationCase


class VolumeTest(IntegrationCase):
    TOTAL = 10_000
    DUPLICATE_SHARE = 0.2

    def _events(self) -> tuple[list[bytes], set[tuple[str, str]], int]:
        """Unique turns across sessions, some continued after a simulated
        /clear (new session id, turn numbering restarts), plus exact
        duplicates of earlier envelopes mixed in."""
        rng = random.Random(1234)
        unique_target = int(self.TOTAL * (1 - self.DUPLICATE_SHARE))
        uniques: list[bytes] = []
        keys: set[tuple[str, str]] = set()
        session = 0
        while len(uniques) < unique_target:
            session += 1
            sid = f"s{session:05d}"
            t = 0
            for _ in range(rng.randint(1, 30)):
                if len(uniques) >= unique_target:
                    break
                if t and rng.random() < 0.05:
                    # /clear: the conversation continues under a new session
                    # id and turn numbering restarts, so turn ids recur.
                    session += 1
                    sid = f"s{session:05d}"
                    t = 0
                tid = f"turn-{t:04d}"
                t += 1
                if (sid, tid) in keys:
                    tid = f"{tid}-b"
                keys.add((sid, tid))
                env = self.envelope(session_id=sid, turn_id=tid, answer_text=f"answer {sid} {tid}")
                uniques.append(json.dumps(env).encode())
        events = list(uniques)
        dupes = self.TOTAL - len(uniques)
        for _ in range(dupes):
            pos = rng.randint(1, len(events))
            src = events[rng.randrange(pos)]  # duplicate of something already sent
            events.insert(pos, src)
        return events, keys, dupes

    def test_expected_unique_episode_and_job_counts(self):
        events, keys, dupes = self._events()
        self.assertEqual(len(events), self.TOTAL)
        # The same turn id recurs under different session ids.
        self.assertGreater(len(keys) - len({k[1] for k in keys}), 1000)
        facts = ingress.git_facts(self.repo)
        env = self.sub_env()
        duplicates_seen = 0
        start = time.monotonic()
        with mock.patch.dict(os.environ, env), mock.patch.object(ingress, "git_facts", return_value=facts):
            for raw in events:
                out = io.StringIO()
                code = ingress.main(io.BytesIO(raw), out, env)
                ack = json.loads(out.getvalue())
                self.assertEqual(code, 0, ack)
                duplicates_seen += ack["duplicate"]
        elapsed = time.monotonic() - start
        print(f"\nvolume: {self.TOTAL} events in {elapsed:.1f} s", file=sys.stderr)

        self.assertEqual(duplicates_seen, dupes)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM episodes"), len(keys))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM jobs WHERE job_type = 'review'"), len(keys))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM jobs"), len(keys))
        self.assertEqual(self.scalar("SELECT COUNT(DISTINCT session_id || '/' || turn_id) FROM episodes"), len(keys))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM capture_errors"), 0)
