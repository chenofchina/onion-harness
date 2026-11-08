"""v2 feature tests - modes, approval gate, MCP generation, task edit/archive."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from onion_harness.core import mcp_contract
from onion_harness.core.modes import (
    ELEVATION_LEVELS,
    all_modes,
    compose_mode_prompt,
    mode_spec,
    plan_mode_violation,
    requires_approval,
)
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineToolCall, EngineUsage
from onion_harness.harness.mcp_factory import McpStore, McpToolSpec, validate_spec


# ------------------------------------------------------------------- modes
class ModeTest(unittest.TestCase):
    def test_three_modes_exist(self):
        self.assertEqual([m["id"] for m in all_modes()], ["plan", "standard", "strict"])

    def test_strict_injects_the_contract(self):
        prompt = compose_mode_prompt("strict", "BASE")
        self.assertIn("Zero-trust", prompt)
        self.assertIn("[Source:", prompt)
        self.assertIn("Self-verification", prompt)
        self.assertIn("Mandatory preview", prompt)
        self.assertTrue(prompt.startswith("BASE"))

    def test_plan_injects_read_only_contract(self):
        prompt = compose_mode_prompt("plan", "BASE")
        self.assertIn("read-only", prompt.lower())

    def test_standard_adds_nothing(self):
        self.assertEqual(compose_mode_prompt("standard", "BASE"), "BASE")

    def test_plan_blocks_writes(self):
        self.assertIsNotNone(plan_mode_violation("plan", "rm -rf build"))
        self.assertIsNotNone(plan_mode_violation("plan", "npm install left-pad"))
        self.assertIsNotNone(plan_mode_violation("plan", "Set-Content -Path x -Value y"))
        self.assertIsNone(plan_mode_violation("plan", "print('read only')"))

    def test_standard_allows_writes_but_asks(self):
        self.assertIsNone(plan_mode_violation("standard", "rm -rf x"))
        self.assertTrue(requires_approval("standard", "rm -rf x"))
        self.assertFalse(requires_approval("standard", "print(1)"))

    def test_strict_never_blocks_inline(self):
        self.assertFalse(requires_approval("strict", "rm -rf x"))
        self.assertIsNone(plan_mode_violation("strict", "rm -rf x"))

    def test_elevation_levels(self):
        ids = [level["id"] for level in ELEVATION_LEVELS]
        self.assertEqual(ids, ["standard", "ask", "elevated"])

    def test_mode_spec_falls_back_to_standard(self):
        self.assertEqual(mode_spec("nonsense").id, "standard")
        self.assertEqual(mode_spec(None).id, "standard")


# -------------------------------------------------------------- mcp contract
class McpContractTest(unittest.TestCase):
    def test_accepts_a_valid_spec(self):
        validate_spec(
            "word_stats",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            "def handle(arguments):\n    return 'ok'\n",
        )

    def test_rejects_bad_names(self):
        for name in ("Bad", "1abc", "a", "with space", ""):
            with self.assertRaises(mcp_contract.McpError):
                validate_spec(name, {"type": "object"}, "def handle(a):\n    return 1\n")

    def test_requires_a_handle_function(self):
        with self.assertRaises(mcp_contract.McpError):
            validate_spec("tool_x", {"type": "object"}, "def other():\n    return 1\n")

    def test_rejects_network_imports(self):
        for module in ("socket", "urllib", "requests", "subprocess", "shutil"):
            with self.assertRaises(mcp_contract.McpError):
                validate_spec(
                    "net_tool",
                    {"type": "object"},
                    f"import {module}\ndef handle(a):\n    return 1\n",
                )

    def test_rejects_dynamic_execution(self):
        with self.assertRaises(mcp_contract.McpError):
            validate_spec("dyn", {"type": "object"}, "def handle(a):\n    return eval('1')\n")

    def test_rejects_shell_attributes(self):
        with self.assertRaises(mcp_contract.McpError):
            validate_spec(
                "shell", {"type": "object"}, "import os\ndef handle(a):\n    return os.system('x')\n"
            )

    def test_rejects_syntax_errors(self):
        with self.assertRaises(mcp_contract.McpError):
            validate_spec("broken", {"type": "object"}, "def handle(:\n")


class McpStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="onion-mcp-"))
        self.store = McpStore(self.root)

    def spec(self, name="word_stats"):
        return McpToolSpec(
            name=name,
            title=name.replace("_", " ").title(),
            description="desc",
            parameters={"type": "object", "properties": {}},
            code="def handle(arguments):\n    return {'ok': True}\n",
        )

    def test_write_creates_script_and_index(self):
        spec = self.spec()
        script = self.store.write_tool(spec)
        self.store.upsert(spec)
        self.assertTrue(script.is_file())
        self.assertIn("def handle(arguments)", script.read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in self.store.load_index()], ["word_stats"])

    def test_script_is_runnable_python(self):
        spec = self.spec()
        script = self.store.write_tool(spec)
        import ast

        ast.parse(script.read_text(encoding="utf-8"))

    def test_enable_disable(self):
        spec = self.spec()
        self.store.write_tool(spec)
        self.store.upsert(spec)
        self.assertEqual(len(self.store.enabled_entries()), 1)
        self.store.set_enabled("word_stats", False)
        self.assertEqual(self.store.enabled_entries(), [])

    def test_remove(self):
        spec = self.spec()
        self.store.write_tool(spec)
        self.store.upsert(spec)
        self.store.remove("word_stats")
        self.assertEqual(self.store.load_index(), [])
        self.assertFalse(self.store.script_exists("word_stats"))


# --------------------------------------------------- runtime mode behaviour
class _ScriptedProvider:
    def __init__(self, script):
        self.model = "fake"
        self.last_reasoning = ""
        self._script = list(script)
        self._n = 0

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        self._n += 1
        step = self._script[min(self._n - 1, len(self._script) - 1)]
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=self.model,
        )


class RuntimeModeTest(unittest.TestCase):
    def setUp(self):
        self._orig = runtime_module.build_provider
        self.dir = Path(tempfile.mkdtemp(prefix="onion-v2-"))
        from onion_harness.harness.runtime import OnionRuntime
        from onion_harness.ledger import ChangeLedger, LedgerStore
        from onion_harness.plugins import builtin_executors
        from onion_harness.sandbox import Sandbox

        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self.runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )

    def tearDown(self):
        runtime_module.build_provider = self._orig
        self.ledger.close()

    def run_with(self, mode, code):
        runtime_module.build_provider = lambda *a, **k: _ScriptedProvider(
            [
                {"tool_calls": [EngineToolCall(id="t1", name="run_python", arguments={"code": code})]},
                {"text": "done"},
            ]
        )
        from onion_harness.core import Task
        from onion_harness.harness.runtime import build_model_config

        model = build_model_config(
            {"provider_id": "deepseek", "api_key": "k", "mode": mode},
        )
        events = []

        async def consume():
            async for ev in self.runtime.run_task(Task(prompt="x", workspace=str(self.dir)), model):
                events.append(ev)

        asyncio.run(consume())
        return events

    def test_plan_mode_blocks_writes(self):
        events = self.run_with("plan", "open('x.txt','w').write('hi')")
        results = [e for e in events if e.type.value == "tool-result"]
        self.assertTrue(results)
        self.assertIn("Plan mode", results[0].payload["content"])
        self.assertFalse((self.dir / "x.txt").exists())

    def test_plan_mode_allows_reads(self):
        events = self.run_with("plan", "print('reading')")
        results = [e for e in events if e.type.value == "tool-result"]
        self.assertEqual(results[0].payload["status"], "success")
        self.assertIn("reading", results[0].payload["content"])

    def test_strict_mode_marks_results_verified(self):
        events = self.run_with("strict", "print('evidence')")
        results = [e for e in events if e.type.value == "tool-result"]
        self.assertTrue(results[0].payload["verified"])

    def test_standard_mode_does_not_mark_verified(self):
        events = self.run_with("standard", "print('plain')")
        results = [e for e in events if e.type.value == "tool-result"]
        self.assertFalse(results[0].payload.get("verified"))

    def test_strict_prompt_reaches_the_system_message(self):
        prompt = self.runtime._compose_system_prompt(self.ledger.state, mode="strict")
        self.assertIn("Zero-trust", prompt)

    def test_approval_can_be_resolved_from_another_thread(self):
        """A pending Standard-mode write waits, then proceeds when approved."""
        import threading

        runtime_module.build_provider = lambda *a, **k: _ScriptedProvider(
            [
                {"tool_calls": [EngineToolCall(id="t1", name="run_python", arguments={"code": "open('ok.txt','w').write('y')"})]},
                {"text": "done"},
            ]
        )
        from onion_harness.core import Task
        from onion_harness.harness.runtime import build_model_config

        model = build_model_config({"provider_id": "deepseek", "api_key": "k", "mode": "standard"})
        task = Task(prompt="x", workspace=str(self.dir))

        events = []
        seen: list = []
        original = self.runtime._request_approval

        async def auto_approve(executor, args, session):
            """Stand-in for the user clicking Approve, so the test never hangs."""
            seen.append((executor.name, args.get("code")))
            return True

        self.runtime._request_approval = auto_approve

        async def consume():
            async for ev in self.runtime.run_task(task, model):
                events.append(ev)

        try:
            asyncio.run(consume())
        finally:
            self.runtime._request_approval = original

        # The gate was consulted before the write happened.
        self.assertTrue(seen)
        results = [e for e in events if e.type.value == "tool-result"]
        self.assertEqual(results[0].payload["status"], "success")
        self.assertTrue((self.dir / "ok.txt").exists())


# --------------------------------------------------------- service features
class ServiceFeatureTest(unittest.TestCase):
    def setUp(self):
        from onion_harness.api import OnionService

        self.original = runtime_module.build_provider
        self.dir = Path(tempfile.mkdtemp(prefix="onion-svc2-"))
        self.service = OnionService(data_dir=self.dir)
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self.original

    def _mcp_tool_names(self):
        """Only the MCP-managed tools; web search shares the same list."""
        return [
            tool.name
            for tool in self.service.runtime._extra_tools
            if (tool.metadata or {}).get("kind") != "web-search"
        ]

    def test_modes_payload(self):
        payload = self.service.modes()
        self.assertEqual(len(payload["modes"]), 3)
        self.assertEqual(len(payload["elevation_levels"]), 3)
        self.assertEqual(payload["thinking_slider"]["stops"][0], "off")

    def test_settings_persist_mode_and_elevation(self):
        self.service.update_settings({"mode": "strict", "elevation": "elevated"})
        reloaded = self.service.store.load_settings()
        self.assertEqual(reloaded["mode"], "strict")
        self.assertEqual(reloaded["elevation"], "elevated")

    def test_mcp_tool_lifecycle(self):
        change_id = self.service.propose_change(
            {
                "kind": "register_mcp_tool",
                "target": "word_stats",
                "reason": "test",
                "params": {
                    "name": "word_stats",
                    "title": "Word Stats",
                    "description": "Count words",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
                    "code": "def handle(arguments):\n    return len(str(arguments.get('text','')).split())\n",
                },
            }
        )["change_id"]
        self.service.apply_change(change_id, {})

        tools = self.service.mcp_tools()
        self.assertEqual([t["name"] for t in tools], ["word_stats"])
        self.assertTrue((self.dir / "mcp" / "word_stats" / "tool.py").is_file())

        # Hot reload: disabling drops it from the live tool set. Only the MCP
        # tools are counted here - web search is injected into the same list but
        # is not managed by the MCP store.
        self.service.set_mcp_enabled("word_stats", False)
        self.assertEqual(self._mcp_tool_names(), [])
        self.service.set_mcp_enabled("word_stats", True)
        self.assertEqual(self._mcp_tool_names(), ["word_stats"])

        # Deletion removes files and state.
        self.service.delete_mcp_tool("word_stats")
        self.assertFalse((self.dir / "mcp" / "word_stats" / "tool.py").exists())

    def test_mcp_rollback_removes_the_tool(self):
        change_id = self.service.propose_change(
            {
                "kind": "register_mcp_tool",
                "target": "tmp_tool",
                "reason": "test",
                "params": {
                    "name": "tmp_tool",
                    "description": "temporary",
                    "parameters": {"type": "object", "properties": {}},
                    "code": "def handle(arguments):\n    return 1\n",
                },
            }
        )["change_id"]
        revision = self.service.apply_change(change_id, {})
        self.assertTrue((self.dir / "mcp" / "tmp_tool" / "tool.py").exists())

        self.service.revert_version(revision["version"])
        self.assertEqual(self.service.ledger.state.get("mcp_tools"), [])
        self.assertEqual(self._mcp_tool_names(), [])

    def test_archive_and_restore(self):
        runtime_module.build_provider = lambda *a, **k: _ScriptedProvider([{"text": "hi"}])
        record = self.service.create_task({"prompt": "archive me"})
        for _ in range(200):
            if any(e["type"] == "task-done" for e in self.service.task_events(record["id"])):
                break
            time.sleep(0.03)

        self.service.archive_task(record["id"], True)
        self.assertEqual([t["id"] for t in self.service.store.load_tasks() if t.get("archived")], [record["id"]])
        self.assertEqual(len(self.service.archived_tasks()), 1)

        self.service.archive_task(record["id"], False)
        self.assertEqual(self.service.archived_tasks(), [])

    def test_edit_task_reruns_with_the_same_id(self):
        runtime_module.build_provider = lambda *a, **k: _ScriptedProvider([{"text": "first"}])
        record = self.service.create_task({"prompt": "original"})
        for _ in range(200):
            if any(e["type"] == "task-done" for e in self.service.task_events(record["id"])):
                break
            time.sleep(0.03)

        edited = self.service.edit_task(record["id"], "revised prompt")
        self.assertEqual(edited["id"], record["id"])
        self.assertEqual(edited["prompt"], "revised prompt")
        self.assertEqual(edited["edit_count"], 1)

        for _ in range(200):
            events = self.service.task_events(record["id"])
            if sum(1 for e in events if e["type"] == "task-done") >= 2:
                break
            time.sleep(0.03)
        user_events = [e for e in self.service.task_events(record["id"]) if e["type"] == "user-input"]
        self.assertTrue(any(e["payload"].get("edited") for e in user_events))

    def test_default_data_dir_is_onionharness(self):
        from onion_harness.api.store import default_data_dir
        import os

        previous = os.environ.pop("ONION_DATA_DIR", None)
        try:
            self.assertTrue(str(default_data_dir()).endswith(".onionharness"))
        finally:
            if previous is not None:
                os.environ["ONION_DATA_DIR"] = previous


if __name__ == "__main__":
    unittest.main()

class ApprovalEventConstructionTest(unittest.TestCase):
    """Regression: the approval payload must not duplicate event()'s task_id.

    The previous test replaced ``_request_approval`` with a stub, which is
    exactly why this bug reached the app: the real function builds the event
    and raised "got multiple values for argument 'task_id'" on every call.
    """

    def setUp(self):
        self._orig = runtime_module.build_provider
        self.dir = Path(tempfile.mkdtemp(prefix="onion-approval-"))
        from onion_harness.harness.runtime import OnionRuntime
        from onion_harness.ledger import ChangeLedger, LedgerStore
        from onion_harness.plugins import builtin_executors
        from onion_harness.sandbox import Sandbox

        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        sandbox = Sandbox(workspace_root=str(self.dir))
        self.runtime = OnionRuntime(
            ledger=self.ledger,
            executors=builtin_executors(sandbox),
            sandbox=sandbox,
            state_provider=lambda: self.ledger.state,
        )

    def tearDown(self):
        runtime_module.build_provider = self._orig
        self.ledger.close()

    def test_record_approval_builds_an_event_without_typeerror(self):
        from onion_harness.core import Task
        from onion_harness.harness.runtime import ModelConfig, TaskSession
        from onion_harness.core.events import EventType

        task = Task(prompt="x", workspace=str(self.dir))
        session = TaskSession(task=task, model=ModelConfig())
        # The exact payload shape _request_approval produces.
        payload = {
            "call_id": "apr_1",
            "name": "run_python",
            "executor": "python",
            "label": "Python",
            "preview": "write a file",
            "code": "open('a','w')",
            "status": "awaiting",
        }
        self.runtime._record_approval(session, payload)

        self.assertEqual(len(session.events), 1)
        recorded = session.events[0]
        self.assertEqual(recorded.type, EventType.TOOL_APPROVAL)
        self.assertEqual(recorded.task_id, task.id)
        self.assertEqual(recorded.payload["call_id"], "apr_1")

    def test_request_approval_emits_through_the_listener(self):
        """Call the real _request_approval and resolve it immediately."""
        import asyncio
        from onion_harness.core import Task
        from onion_harness.harness.runtime import ModelConfig, TaskSession

        task = Task(prompt="x", workspace=str(self.dir))
        session = TaskSession(task=task, model=ModelConfig())
        # resolve_approval looks the session up by task id, so the handle must be
        # registered the way run_task registers it.
        self.runtime._sessions[task.id] = session

        seen = []
        self.runtime.set_approval_listener(lambda task_id, payload: seen.append(payload))

        class FakeExecutor:
            name = "python"
            label = "Python"
            tool_name = "run_python"

            def preview(self, code):
                return code[:40]

        async def go():
            # Approve from a sibling task once the request is pending.
            async def approve_soon():
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    if session.approval_pending:
                        approval_id = next(iter(session.approval_pending))
                        resolved = self.runtime.resolve_approval(task.id, approval_id, True)
                        self.assertTrue(resolved, "resolve_approval found no pending request")
                        return
                self.fail("approval request never became pending")

            approver = asyncio.create_task(approve_soon())
            approved = await self.runtime._request_approval(
                FakeExecutor(), {"code": "print(1)", "description": "run"}, session
            )
            await approver
            return approved

        approved = asyncio.run(go())
        self.assertTrue(approved)
        # Both the request and the resolution reached the listener.
        statuses = [payload["payload"]["status"] for payload in seen]
        self.assertIn("awaiting", statuses)
        self.assertIn("approved", statuses)
