"""Layer 1 tests - core primitives stay pure and closed."""

from __future__ import annotations

import dataclasses
import unittest

from onion_harness.core import (
    Context,
    EventType,
    Layer,
    OnionEvent,
    OnionTask,
    OnionTool,
    Result,
    Task,
    TaskKind,
)


class LayerOrderingTest(unittest.TestCase):
    def test_depth_increases_outward(self):
        self.assertLess(Layer.CORE.depth, Layer.SANDBOX.depth)
        self.assertLess(Layer.SANDBOX.depth, Layer.HARNESS.depth)
        self.assertLess(Layer.HARNESS.depth, Layer.API.depth)
        self.assertLess(Layer.API.depth, Layer.GUI.depth)

    def test_exactly_six_layers(self):
        self.assertEqual(len(list(Layer)), 6)


class TaskTest(unittest.TestCase):
    def test_defaults_are_agent_non_elevated(self):
        task = Task(prompt="hi")
        self.assertEqual(task.kind, TaskKind.AGENT)
        self.assertFalse(task.elevate)
        self.assertTrue(task.id.startswith("task_"))

    def test_derive_keeps_identity(self):
        task = Task(prompt="hi")
        derived = task.derive(prompt="changed", elevate=True)
        self.assertEqual(derived.id, task.id)
        self.assertTrue(derived.elevate)
        self.assertEqual(task.prompt, "hi")  # original untouched

    def test_elevate_is_per_task(self):
        a, b = Task(prompt="a"), Task(prompt="b", elevate=True)
        self.assertFalse(a.elevate)
        self.assertTrue(b.elevate)

    def test_config_has_no_engine_naming(self):
        names = " ".join(f.name for f in dataclasses.fields(Task)).lower()
        self.assertNotIn("loong", names)
        # Elevation is a task property, never a runtime-level concept.
        self.assertIn("elevate", names)
        self.assertNotIn("uac", names)

    def test_to_dict_is_json_safe(self):
        import json

        payload = Task(kind=TaskKind.COMMAND, command=["python", "-c", "1"]).to_dict()
        json.dumps(payload)
        self.assertEqual(payload["kind"], "command")


class ContextTest(unittest.TestCase):
    def test_for_task_copies_task_settings(self):
        task = Task(prompt="x", workspace="C:/tmp", timeout_seconds=5, elevate=True)
        ctx = Context.for_task(task, Layer.SANDBOX)
        self.assertEqual(ctx.task_id, task.id)
        self.assertEqual(ctx.layer, Layer.SANDBOX)
        self.assertEqual(ctx.workspace, "C:/tmp")
        self.assertTrue(ctx.elevate)

    def test_cancellation(self):
        class E:
            def __init__(self, value):
                self.value = value

            def is_set(self):
                return self.value

        self.assertFalse(Context(task_id="t", cancel=E(False)).cancellation_requested())
        self.assertTrue(Context(task_id="t", cancel=E(True)).cancellation_requested())
        self.assertFalse(Context(task_id="t").cancellation_requested())


class ResultTest(unittest.TestCase):
    def test_exit_code_zero_is_success(self):
        result = Result.from_exit(0, stdout="ok")
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)

    def test_nonzero_is_failure(self):
        result = Result.from_exit(2, stderr="bad")
        self.assertFalse(result.ok)
        self.assertEqual(result.summary, "exit code 2")

    def test_timeout_summary(self):
        self.assertEqual(Result(timed_out=True).summary, "execution timed out")


class EventTest(unittest.TestCase):
    def test_roundtrip(self):
        ev = OnionEvent(type=EventType.TOOL_CALL, task_id="t1", seq=3, payload={"name": "x"})
        restored = OnionEvent.from_dict(ev.to_dict())
        self.assertEqual(restored.type, EventType.TOOL_CALL)
        self.assertEqual(restored.payload, {"name": "x"})
        self.assertEqual(restored.seq, 3)

    def test_payload_is_json_safe(self):
        import json

        ev = OnionEvent(type=EventType.ERROR, task_id="t", payload={"message": "boom", "code": "x"})
        json.dumps(ev.to_dict())

    def test_all_card_types_exist(self):
        values = {t.value for t in EventType}
        for required in [
            "user-input",
            "reasoning",
            "tool-call",
            "tool-result",
            "change-proposal",
            "change-applied",
            "change-request",
            "error",
        ]:
            self.assertIn(required, values)


class ToolTest(unittest.TestCase):
    def test_rejects_invalid_names(self):
        with self.assertRaises(ValueError):
            OnionTool(name="bad name!", description="d", input_schema={}, handler=lambda a: a)

    def test_accepts_snake_case(self):
        tool = OnionTool(name="run_python", description="d", input_schema={}, handler=lambda a: a)
        self.assertEqual(tool.name, "run_python")

    def test_alias_identity(self):
        self.assertIs(OnionTask, Task)


if __name__ == "__main__":
    unittest.main()