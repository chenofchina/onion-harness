"""Layer 4 tests - executors and their unified output envelope."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from onion_harness.core import Context, Layer, Task, TaskKind
from onion_harness.plugins import builtin_executors
from onion_harness.plugins.registry import executor_class
from onion_harness.sandbox import Sandbox


def run(coro):
    return asyncio.run(coro)


class EnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="onion-exec-")
        self.sandbox = Sandbox(workspace_root=self.workspace)
        self.registry = builtin_executors(self.sandbox)
        self.ctx = Context(task_id="t", layer=Layer.PLUGINS, workspace=self.workspace)

    def _exec(self, name, code):
        return run(self.registry.get(name).execute({"code": code}, self.ctx))

    def test_envelope_shape(self):
        outcome = self._exec("python", "print('ok')")
        output = outcome.output
        self.assertEqual(set(output) >= {"stream", "data", "timestamp", "status"}, True)
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["stream"], "stdout")
        self.assertIn("executor", output["data"])
        self.assertEqual(output["data"]["executor"], "python")
        self.assertIsInstance(output["timestamp"], float)

    def test_python_executor(self):
        outcome = self._exec("python", "print(6*7)")
        self.assertTrue(outcome.ok)
        self.assertIn("42", outcome.result.stdout)
        self.assertIn("42", outcome.model_text())

    def test_python_reports_errors(self):
        outcome = self._exec("python", "raise ValueError('boom')")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.output["status"], "failed")
        self.assertIn("ValueError", outcome.result.stderr)
        self.assertIn("boom", outcome.model_text())

    def test_empty_code_rejected(self):
        outcome = self._exec("python", "   ")
        self.assertFalse(outcome.ok)
        self.assertIn("empty", outcome.result.error or "")

    @unittest.skipUnless(os.name == "nt", "Windows-only executor")
    def test_powershell_executor(self):
        outcome = self._exec("powershell", "Write-Output (21*2)")
        self.assertTrue(outcome.ok)
        self.assertIn("42", outcome.result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows-only executor")
    def test_cmd_executor(self):
        outcome = self._exec("cmd", "echo hello-cmd")
        self.assertTrue(outcome.ok)
        self.assertIn("hello-cmd", outcome.result.stdout)

    def test_script_file_is_cleaned_up(self):
        import pathlib

        alphabet_before = set(pathlib.Path(self.workspace).iterdir())
        self._exec("python", "print('cleanup')")
        alphabet_after = set(pathlib.Path(self.workspace).iterdir())
        leftovers = [p.name for p in alphabet_after - alphabet_before if p.name.startswith(".onion-")]
        self.assertEqual(leftovers, [])

    def test_two_executors_can_report_different_streams(self):
        outcome = self._exec("python", "import sys; print('out'); print('err', file=sys.stderr)")
        self.assertEqual(outcome.output["stream"], "mixed")


class RegistryTest(unittest.TestCase):
    def test_catalogue(self):
        sandbox = Sandbox()
        registry = builtin_executors(sandbox)
        self.assertEqual(set(registry.names()), {"python", "powershell", "cmd"})
        described = {d["name"]: d for d in registry.describe()}
        self.assertEqual(described["python"]["tool_name"], "run_python")
        self.assertEqual(described["cmd"]["icon"], "cmd")

    def test_tools_carry_executor_metadata(self):
        sandbox = Sandbox()
        registry = builtin_executors(sandbox)
        tools = registry.tools(lambda executor: (lambda args: "x"))
        self.assertEqual(len(tools), 3)
        for tool in tools:
            self.assertEqual(tool.metadata["kind"], "executor")
            self.assertIn(tool.metadata["executor"], {"python", "powershell", "cmd"})
            self.assertIn("code", tool.input_schema["properties"])

    def test_executor_class_lookup_is_static(self):
        self.assertIsNotNone(executor_class("python"))
        self.assertIsNone(executor_class("../../../etc/passwd"))

    def test_unregister(self):
        registry = builtin_executors(Sandbox())
        registry.unregister("cmd")
        self.assertNotIn("cmd", registry.names())


class ElevationPlumbingTest(unittest.TestCase):
    def test_executor_forwards_elevation_without_reading_it(self):
        workspace = tempfile.mkdtemp(prefix="onion-elev-")
        sandbox = Sandbox(workspace_root=workspace)
        executor = builtin_executors(sandbox).get("python")
        ctx = Context(task_id="t", layer=Layer.PLUGINS, workspace=workspace, elevate=False)
        outcome = run(executor.execute({"code": "print('plain')"}, ctx))
        self.assertTrue(outcome.ok)
        # No elevation key in the envelope: elevation is a launcher concern.
        self.assertNotIn("elevated", outcome.output)
        self.assertNotIn("uac", str(outcome.output).lower())


if __name__ == "__main__":
    unittest.main()