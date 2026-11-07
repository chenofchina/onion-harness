"""Architecture tests - the CI guard, enforced from pytest too.

These run the same checks as ``scripts/check_layers.py`` so a broken dependency
direction fails the normal test command, not only the CI script.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_layers.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_layers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ArchitectureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checker = load_checker()

    def test_dependency_direction(self):
        problems = self.checker.check_direction()
        self.assertEqual(problems, [], "\n".join(problems))

    def test_core_has_no_dependencies(self):
        problems = self.checker.check_core_purity()
        self.assertEqual(problems, [], "\n".join(problems))

    def test_engine_name_is_contained(self):
        problems = self.checker.check_engine_containment()
        self.assertEqual(problems, [], "\n".join(problems))

    def test_gui_does_not_import_python(self):
        problems = self.checker.check_gui_boundary()
        self.assertEqual(problems, [], "\n".join(problems))

    def test_all_layers_exist(self):
        package = ROOT / "onion_harness"
        for layer in ("core", "sandbox", "ledger", "plugins", "harness", "api", "app"):
            self.assertTrue((package / layer / "__init__.py").exists(), layer)

    def test_public_types_are_exported(self):
        import onion_harness

        for name in ("OnionTask", "OnionEvent", "OnionTool", "OnionRuntime"):
            self.assertTrue(hasattr(onion_harness, name), name)

    def test_outer_layers_can_be_imported_without_importing_inner_ones_first(self):
        """Each layer is independently importable in a clean process."""
        import subprocess

        for module in (
            "onion_harness.core",
            "onion_harness.sandbox",
            "onion_harness.plugins",
            "onion_harness.harness",
            "onion_harness.api",
        ):
            result = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"{module}: {result.stderr}")


class PublicSurfaceTest(unittest.TestCase):
    def test_runtime_exposes_no_apply_capability(self):
        """The runtime façade must not let a caller confirm a change."""
        from onion_harness.harness.runtime import OnionRuntime

        public = {name for name in dir(OnionRuntime) if not name.startswith("_")}
        for forbidden in ("apply_change", "confirm", "commit", "approve"):
            self.assertNotIn(forbidden, public)

    def test_service_apply_is_the_user_gate(self):
        """Only the API layer holds apply/revert, i.e. the user-facing gate."""
        from onion_harness.api.service import OnionService

        public = {name for name in dir(OnionService) if not name.startswith("_")}
        self.assertIn("apply_change", public)
        self.assertIn("reject_change", public)
        self.assertIn("revert_version", public)

    def test_harness_surface_is_small(self):
        from onion_harness import harness

        self.assertEqual(
            set(harness.__all__),
            {"OnionRuntime", "ModelConfig", "TaskSession", "build_model_config"},
        )


if __name__ == "__main__":
    unittest.main()