import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotune import paths


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class ProjectIdTest(unittest.TestCase):
    def test_is_16_lowercase_hex_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid = paths.project_id(tmp)
            self.assertRegex(pid, r"^[0-9a-f]{16}$")
            self.assertEqual(pid, paths.project_id(tmp))

    def test_symlinked_and_unnormalized_paths_share_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "repo" / ".git"
            real.mkdir(parents=True)
            link = Path(tmp) / "link"
            link.symlink_to(Path(tmp) / "repo")
            expected = paths.project_id(str(real))
            self.assertEqual(paths.project_id(str(link / ".git")), expected)
            self.assertEqual(paths.project_id(str(real) + "/"), expected)
            self.assertEqual(paths.project_id(str(real / ".." / ".git")), expected)

    def test_different_dirs_differ(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertNotEqual(paths.project_id(a), paths.project_id(b))


class DataDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home" / "autotune"
        patcher = mock.patch.dict(os.environ, {"AUTOTUNE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.gitdir = Path(self._tmp.name) / "repo" / ".git"
        self.gitdir.mkdir(parents=True)

    def test_base_dir_honors_env(self):
        self.assertEqual(paths.base_dir(), self.home)

    def test_base_dir_default_is_under_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTOTUNE_HOME", None)
            self.assertEqual(paths.base_dir(), Path.home() / ".claude" / "autotune")

    def test_project_data_dir_created_private_under_base(self):
        old = os.umask(0o022)
        try:
            d = paths.project_data_dir(str(self.gitdir))
        finally:
            os.umask(old)
        self.assertTrue(d.is_dir())
        self.assertEqual(d.parent, self.home)
        self.assertEqual(d.name, paths.project_id(str(self.gitdir)))
        self.assertEqual(mode(d), 0o700)
        self.assertEqual(mode(self.home), 0o700)

    def test_project_data_dir_is_idempotent_and_repairs_mode(self):
        d = paths.project_data_dir(str(self.gitdir))
        os.chmod(d, 0o755)
        again = paths.project_data_dir(str(self.gitdir))
        self.assertEqual(d, again)
        self.assertEqual(mode(d), 0o700)

    def test_file_locations_stay_inside_data_dir(self):
        d = paths.project_data_dir(str(self.gitdir))
        self.assertEqual(paths.db_path(d), d / "autotune.sqlite3")
        self.assertEqual(paths.worker_lock_path(d), d / "worker.lock")
        self.assertEqual(paths.project_meta_path(d), d / "project.json")
        log = paths.log_path(d)
        self.assertEqual(log, d / "logs" / "autotune.log")
        self.assertTrue(log.parent.is_dir())
        self.assertEqual(mode(log.parent), 0o700)

    def test_machine_lock_is_shared_across_projects(self):
        self.assertEqual(paths.machine_lock_path(), self.home / "experiment.lock")


if __name__ == "__main__":
    unittest.main()
