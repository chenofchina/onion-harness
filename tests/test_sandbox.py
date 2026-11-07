"""Layer 2 tests - the sandbox contract and isolation behaviour."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from onion_harness.core import Context, Layer, Result, Task, TaskKind
from onion_harness.sandbox import Sandbox, ProcessIsolationStrategy
from onion_harness.sandbox.api import resolve_workspace, SandboxError


def run(coro):
    return asyncio.run(coro)


class SandboxContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onion-sbx-")
        self.sandbox = Sandbox(workspace_root=self.tmp)

    def test_echo_roundtrip(self):
        task = Task(
            kind=TaskKind.COMMAND,
            command=[sys.executable, "-c", "print('hello sandbox')"],
        )
        result = run(self.sandbox.execute(task))
        self.assertTrue(result.ok)
        self.assertIn("hello sandbox", result.stdout)
        self.assertEqual(result.exit_code, 0)

    def test_nonzero_exit_is_reported(self):
        task = Task(
            kind=TaskKind.COMMAND,
            command=[sys.executable, "-c", "import sys; sys.exit(7)"],
        )
        result = run(self.sandbox.execute(task))
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 7)

    def test_timeout_kills_process(self):
        task = Task(
            kind=TaskKind.COMMAND,
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.7,
        )
        result = run(self.sandbox.execute(task))
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)

    def test_cancellation(self):
        class Cancel:
            def is_set(self):
                return True

        task = Task(kind=TaskKind.COMMAND, command=[sys.executable, "-c", "print(1)"])
        result = run(self.sandbox.execute(task, Context(task_id=task.id, cancel=Cancel())))
        self.assertTrue(result.cancelled)

    def test_agent_task_is_refused(self):
        result = run(self.sandbox.execute(Task(prompt="not a command")))
        self.assertFalse(result.ok)
        self.assertIn("command", result.error)

    def test_missing_executable(self):
        task = Task(kind=TaskKind.COMMAND, command=["definitely-not-a-real-binary-xyz"])
        result = run(self.sandbox.execute(task))
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error or "")


class WorkspaceIsolationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="onion-root-"))
        self.sandbox = Sandbox(workspace_root=str(self.root))

    def test_cwd_is_forced_to_workspace(self):
        task = Task(
            kind=TaskKind.COMMAND,
            command=[sys.executable, "-c", "import os; print(os.getcwd())"],
            
        )
        result = run(self.sandbox.execute(task))
        self.assertTrue(result.ok)
        self.assertEqual(
            Path(result.stdout.strip()).resolve(), Path(self.root).resolve()
        )

    def test_workspace_outside_root_is_refused(self):
        task = Task(
            kind=TaskKind.COMMAND,
            command=[sys.executable, "-c", "print(1)"],
            workspace="C:/Windows",
        )
        result = run(self.sandbox.execute(task))
        self.assertFalse(result.ok)
        self.assertIn("outside", result.error or "")

    def test_resolve_workspace_creates_directory(self):
        target = resolve_workspace("nested/deep", self.root)
        self.assertTrue(target.is_dir())
        self.assertTrue(str(target).startswith(str(self.root.resolve())))

    def test_resolve_workspace_rejects_escape(self):
        with self.assertRaises(SandboxError):
            resolve_workspace("../../escape", self.root)


class OutputBoundingTest(unittest.TestCase):
    def test_large_output_is_truncated(self):
        sandbox = Sandbox(output_limit=1000)
        task = Task(
            kind=TaskKind.COMMAND,
            command=[sys.executable, "-c", "print('x' * 50000)"],
        )
        result = run(sandbox.execute(task))
        self.assertTrue(result.truncated)
        self.assertLess(len(result.stdout), 50000)
        self.assertIn("truncated", result.stdout)


class StrategySwappabilityTest(unittest.TestCase):
    def test_strategy_can_be_replaced(self):
        root = tempfile.mkdtemp(prefix="onion-strat-")
        sandbox = Sandbox(workspace_root=root)
        swapped = sandbox.with_strategy(ProcessIsolationStrategy(root))
        task = Task(kind=TaskKind.COMMAND, command=[sys.executable, "-c", "print('swapped')"])
        result = run(swapped.execute(task))
        self.assertTrue(result.ok)
        self.assertIn("swapped", result.stdout)

    def test_execute_is_the_only_public_entry(self):
        public = {name for name in dir(Sandbox) if not name.startswith("_")}
        # Deliberately tiny surface: one entry point plus config helpers.
        for forbidden in ("run", "spawn", "popen", "shell"):
            self.assertNotIn(forbidden, public)


class ElevationIsolationTest(unittest.TestCase):
    def test_elevation_defaults_off(self):
        self.assertFalse(Task(kind=TaskKind.COMMAND, command=["x"]).elevate)

    def test_result_never_exposes_elevation_fields(self):
        fields = set(Result.__dataclass_fields__)
        self.assertNotIn("elevated", fields)
        self.assertNotIn("uac", fields)


if __name__ == "__main__":
    unittest.main()