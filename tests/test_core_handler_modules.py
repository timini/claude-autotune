"""Handler module loading: an absent module is recorded as missing, a broken one as an import error."""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from autotune import worker


class HandlerModuleLoadingTest(unittest.TestCase):
    def load(self, modules: dict) -> tuple[list, list]:
        errors, missing = [], []
        with mock.patch.object(worker, "HANDLER_MODULES", tuple(modules)), \
                mock.patch.object(worker, "HANDLER_IMPORT_ERRORS", errors), \
                mock.patch.object(worker, "HANDLER_MODULES_MISSING", missing), \
                mock.patch.dict(sys.modules, {f"autotune.{n}": m for n, m in modules.items()}):
            worker._load_handler_modules()
        return errors, missing

    def test_absent_module_is_missing_not_an_error(self):
        # A None entry in sys.modules makes import raise ModuleNotFoundError for that name.
        errors, missing = self.load({"no_such_handler_module": None})
        self.assertEqual((errors, missing), ([], ["no_such_handler_module"]))

    def test_module_importing_something_missing_is_an_error(self):
        dep = "autotune._definitely_missing_dependency"
        exc = ModuleNotFoundError(f"No module named {dep!r}", name=dep)
        with mock.patch("importlib.import_module", side_effect=exc):
            errors, missing = self.load({"evaluate_like": mock.Mock()})
        self.assertEqual(missing, [])
        self.assertEqual([e[0] for e in errors], ["evaluate_like"])
        self.assertIn("ModuleNotFoundError", errors[0][1])

    def test_handler_modules_are_registered(self):
        self.assertEqual(worker.HANDLER_MODULES, ("review", "propose", "evaluate", "apply", "consolidate"))


if __name__ == "__main__":
    unittest.main()
