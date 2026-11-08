"""Interpreter resolution and the review/approval decision flow.

Two defects drove this file.

**The Python executor handed the script to the harness itself.** In a frozen build
``sys.executable`` is ``OnionHarness.exe``, so the command became
``[OnionHarness.exe, "-u", script.py]`` - Onion received its own script path as a
command-line argument and no Python ever ran. It works perfectly in a source
checkout, which is why it survived so long.

**Review-then-approval asked the user twice over.** A snippet the reviewer cleared
still needed a click, which trains people to approve without reading - the exact
behaviour the review exists to prevent. The rule is now: pass runs, flagged asks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from onion_harness.core import EventType
from onion_harness.core.process import (
    CREATE_NO_WINDOW,
    cmd_argv,
    find_python,
    powershell_argv,
    python_argv,
    spawn_kwargs,
)
from onion_harness.harness.bridge import EngineChatResponse, EngineToolCall, EngineUsage
from onion_harness.ledger import ChangeLedger, LedgerStore
from onion_harness.plugins import builtin_executors
from onion_harness.sandbox import Sandbox


class FindPythonTest(unittest.TestCase):
    def test_override_wins(self):
        with mock.patch.dict(os.environ, {"ONION_PYTHON": "/custom/python"}):
            self.assertEqual(find_python(), "/custom/python")

    def test_source_runtime_uses_its_own_interpreter(self):
        """In a venv that is the environment with the user's packages installed."""
        if getattr(sys, "frozen", False):
            self.skipTest("this build is frozen")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ONION_PYTHON", None)
            found = find_python()
        self.assertTrue(
            found.lower().endswith(("python.exe", "python", "python3")),
            f"unexpected interpreter: {found}",
        )

    def test_frozen_build_never_returns_the_harness_binary(self):
        """The regression: a frozen sys.executable is Onion, not Python."""
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", r"C:\Apps\Onion\OnionHarness.exe"
        ), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ONION_PYTHON", None)
            found = find_python()
        self.assertNotIn("OnionHarness", found)
        self.assertNotEqual(found, r"C:\Apps\Onion\OnionHarness.exe")

    def test_a_renamed_embedder_is_not_trusted(self):
        """Only something that looks like Python is accepted as sys.executable."""
        with mock.patch.object(sys, "frozen", False, create=True), mock.patch.object(
            sys, "executable", "/usr/local/bin/runner"
        ), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ONION_PYTHON", None)
            found = find_python()
        self.assertNotEqual(found, "/usr/local/bin/runner")

    def test_falls_back_to_a_bare_name_when_nothing_is_found(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch(
            "onion_harness.core.process.shutil.which", return_value=None
        ), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ONION_PYTHON", None)
            self.assertEqual(find_python(), "python")


class ExecutorCommandTest(unittest.TestCase):
    def test_python_executor_puts_the_script_after_the_interpreter(self):
        from onion_harness.plugins.python_executor import PythonExecutor

        argv = PythonExecutor.build_command(
            PythonExecutor, Path("s.py"), 60, Path(".")
        )
        self.assertEqual(argv[0], find_python())
        self.assertIn("-u", argv)
        self.assertEqual(argv[-1], "s.py")
        # The whole point: the script is an argument to Python, not to Onion.
        self.assertEqual(len(argv), 3)

    def test_python_argv_helper_matches(self):
        argv = python_argv("x.py")
        self.assertEqual(argv, [find_python(), "-u", "x.py"])

    def test_shell_argv_is_silent_and_non_interactive(self):
        ps = powershell_argv("a.ps1")
        self.assertIn("-NonInteractive", ps)
        self.assertIn("-NoProfile", ps)
        self.assertEqual(ps[ps.index("-WindowStyle") + 1], "Hidden")
        self.assertEqual(cmd_argv("a.cmd"), ["cmd.exe", "/d", "/c", "a.cmd"])

    def test_spawn_kwargs_hide_the_window(self):
        with mock.patch("onion_harness.core.process._WINDOWS", True):
            kwargs = spawn_kwargs()
            self.assertTrue(kwargs["creationflags"] & CREATE_NO_WINDOW)
            self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)


class _FakeProvider:
    def __init__(self, script):
        self.model = "fake"
        self.last_reasoning = ""
        self._script = script
        self._i = 0

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(5, 2, 7),
            model=self.model,
        )


class _Gate:
    """A review gate with a fixed verdict, no model involved."""

    def __init__(self, decision, unavailable=False):
        self.decision = decision
        self.unavailable = unavailable
        self.calls = 0

    async def review(self, code, tool_name=""):
        from onion_harness.harness.review import ReviewResult

        self.calls += 1
        if self.decision == "skip":
            return ReviewResult("pass", reason="read-only snippet", skipped=True)
        return ReviewResult(
            self.decision,
            reason="destructive operation detected" if self.decision == "reject" else "ok",
            reviewer_model="fake",
            unavailable=self.unavailable,
        )


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-flow-"))
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self.asked = []
        self.answer = True

    def tearDown(self):
        self.ledger.close()

    def _run(self, gate, mode="standard", code="Remove-Item -Recurse C:\\x"):
        from onion_harness.harness import runtime as runtime_module
        from onion_harness.harness.runtime import OnionRuntime, build_model_config

        original = runtime_module.build_provider
        runtime_module.build_provider = lambda *a, **k: _FakeProvider(
            [
                {
                    "tool_calls": [
                        EngineToolCall(id="c1", name="run_powershell", arguments={"code": code})
                    ]
                },
                {"text": "done"},
            ]
        )
        try:
            runtime = OnionRuntime(
                ledger=self.ledger,
                executors=self.executors,
                sandbox=self.sandbox,
                state_provider=lambda: self.ledger.state,
                reviewer=gate,
            )
            runtime.approval_timeout = 2.0

            async def _approval(*args, **kwargs):
                self.asked.append(kwargs.get("reason", ""))
                return self.answer

            runtime._request_approval = _approval
            model = build_model_config(
                {"provider_id": "deepseek", "api_key": "sk-test", "mode": mode}
            )
            events = []

            async def consume():
                from onion_harness.core import Task

                async for ev in runtime.run_task(Task(prompt="x", workspace=str(self.dir)), model):
                    events.append(ev)

            asyncio.run(consume())
            return events
        finally:
            runtime_module.build_provider = original


class ReviewedFlowTest(_Base):
    """Pass runs; flagged asks; unavailable asks and says why."""

    def test_a_pass_does_not_ask(self):
        gate = _Gate("pass")
        self._run(gate)
        self.assertEqual(self.asked, [], "a cleared snippet must run without a prompt")

    def test_a_reject_asks_and_runs_only_if_approved(self):
        gate = _Gate("reject")
        self.answer = True
        events = self._run(gate)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("flagged", self.asked[0])
        self.assertIn(EventType.TOOL_RESULT, [ev.type for ev in events])

    def test_a_reject_declined_does_not_run(self):
        gate = _Gate("reject")
        self.answer = False
        events = self._run(gate)
        results = [ev for ev in events if ev.type is EventType.TOOL_RESULT]
        self.assertTrue(results)
        self.assertIn("declined", results[0].payload["content"])

    def test_an_unavailable_reviewer_asks_and_says_it_is_the_reviewer(self):
        gate = _Gate("reject", unavailable=True)
        self._run(gate)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("could not check", self.asked[0])
        # The card must not blame the code for a service outage.
        self.assertNotIn("flagged", self.asked[0])

    def test_no_reviewer_still_asks_in_standard_mode(self):
        self._run(None)
        self.assertEqual(len(self.asked), 1, "with no gate, approval is the only guard")

    def test_no_reviewer_does_not_ask_in_strict_mode(self):
        self._run(None, mode="strict")
        self.assertEqual(self.asked, [])

    def test_plan_mode_refuses_a_write_without_asking(self):
        gate = _Gate("pass")
        events = self._run(gate, mode="plan")
        self.assertEqual(self.asked, [], "Plan mode must never offer to run a write")
        results = [ev for ev in events if ev.type is EventType.TOOL_RESULT]
        self.assertTrue(results)
        self.assertIn("Plan mode", results[0].payload["content"])

    def test_a_read_only_snippet_never_reaches_review_or_approval(self):
        gate = _Gate("skip")
        events = self._run(gate, code="Get-Date")
        # The gate is consulted (it decides "read-only"), but it reports skip, so
        # neither a review call nor an approval card follows.
        self.assertEqual(gate.calls, 1)
        self.assertEqual(self.asked, [])
        self.assertIn(EventType.TOOL_RESULT, [ev.type for ev in events])

    def test_the_approval_card_carries_the_review_reason(self):
        """The card is emitted out of band (the generator is blocked inside a tool
        call), so the reason is checked where the user actually receives it."""
        gate = _Gate("reject")
        self._run(gate)
        self.assertTrue(self.asked, "the reason must reach the user")
        self.assertIn("flagged", self.asked[0])


if __name__ == "__main__":
    unittest.main()