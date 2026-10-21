import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from autotune.locks import FileLock, LockHeld, holder_pid, is_locked

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

HOLDER = r"""
import sys, time
from autotune.locks import FileLock
lock = FileLock(sys.argv[1])
if not lock.acquire():
    print("refused", flush=True)
    sys.exit(3)
print("locked", flush=True)
hold = float(sys.argv[2])
time.sleep(hold)
lock.release()
print("released", flush=True)
"""

PROBE = r"""
import sys
from autotune.locks import FileLock, is_locked
print("locked" if is_locked(sys.argv[1]) else "free", flush=True)
lock = FileLock(sys.argv[1])
print("acquired" if lock.acquire() else "refused", flush=True)
"""

SPAWNING_HOLDER = r"""
import subprocess, sys, time
from autotune.locks import FileLock
lock = FileLock(sys.argv[1])
assert lock.acquire()
print("locked", flush=True)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    close_fds=False, stdin=subprocess.DEVNULL, start_new_session=True,
)
print(child.pid, flush=True)
time.sleep(60)
"""


def child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


class LockTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "worker.lock"
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            if p.stdout:
                p.stdout.close()

    def spawn(self, source, *args):
        p = subprocess.Popen(
            [sys.executable, "-c", source, str(self.path), *map(str, args)],
            cwd=REPO_ROOT,
            env=child_env(),
            stdout=subprocess.PIPE,
            text=True,
        )
        self.procs.append(p)
        return p

    def spawn_holder(self, hold_seconds=60):
        p = self.spawn(HOLDER, hold_seconds)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        return p

    def run_probe(self):
        out = subprocess.run(
            [sys.executable, "-c", PROBE, str(self.path)],
            cwd=REPO_ROOT, env=child_env(), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.split()


class CrossProcessTest(LockTestBase):
    def test_second_holder_refused_while_first_alive(self):
        holder = self.spawn_holder()
        self.assertTrue(is_locked(self.path))
        self.assertFalse(FileLock(self.path).acquire())
        self.assertEqual(self.run_probe(), ["locked", "refused"])
        self.assertEqual(holder_pid(self.path), holder.pid)

    def test_sigkill_releases_lock(self):
        holder = self.spawn_holder()
        self.assertFalse(FileLock(self.path).acquire())
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        self.assertEqual(holder.returncode, -signal.SIGKILL)
        self.assertFalse(is_locked(self.path))
        lock = FileLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertEqual(holder_pid(self.path), os.getpid())
        lock.release()

    def test_refused_in_child_while_parent_holds(self):
        with FileLock(self.path):
            self.assertEqual(self.run_probe(), ["locked", "refused"])
        self.assertEqual(self.run_probe(), ["free", "acquired"])

    def test_grandchild_does_not_inherit_lock(self):
        holder = self.spawn(SPAWNING_HOLDER)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        grandchild_pid = int(holder.stdout.readline())
        self.addCleanup(self._kill_pid, grandchild_pid)
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        os.kill(grandchild_pid, 0)
        self.assertFalse(is_locked(self.path))

    @staticmethod
    def _kill_pid(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_blocking_acquire_waits_for_release(self):
        holder = self.spawn_holder(hold_seconds=0.5)
        start = time.monotonic()
        lock = FileLock(self.path, blocking=True)
        self.assertTrue(lock.acquire())
        self.assertGreater(time.monotonic() - start, 0.2)
        self.assertEqual(holder.stdout.readline().strip(), "released")
        lock.release()


class InProcessTest(LockTestBase):
    def test_file_mode_and_pid(self):
        old = os.umask(0o022)
        try:
            lock = FileLock(self.path)
            self.assertTrue(lock.acquire())
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(holder_pid(self.path), os.getpid())
        lock.release()
        self.assertIsNone(holder_pid(self.path))
        self.assertTrue(self.path.exists())

    def test_context_manager_raises_when_held(self):
        with FileLock(self.path) as first:
            self.assertTrue(first.held)
            with self.assertRaises(LockHeld):
                with FileLock(self.path):
                    self.fail("body must not run without the lock")
        self.assertFalse(first.held)
        self.assertFalse(is_locked(self.path))

    def test_release_on_exception(self):
        with self.assertRaises(ValueError):
            with FileLock(self.path):
                raise ValueError("boom")
        self.assertFalse(is_locked(self.path))

    def test_release_is_idempotent_and_reacquire_works(self):
        lock = FileLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())
        lock.release()
        lock.release()
        self.assertTrue(lock.acquire())
        lock.release()

    def test_is_locked_does_not_create_file(self):
        self.assertFalse(is_locked(self.path))
        self.assertFalse(self.path.exists())

    def test_probe_leaves_lock_free(self):
        setup = FileLock(self.path)
        self.assertTrue(setup.acquire())
        setup.release()
        self.assertFalse(is_locked(self.path))
        self.assertFalse(is_locked(self.path))
        lock = FileLock(self.path)
        self.assertTrue(lock.acquire())
        lock.release()


if __name__ == "__main__":
    unittest.main()
