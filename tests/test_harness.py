"""Harness tests - context compression, prompt injection, error boundary,
model presets, the Anthropic bridge and the runtime wrapper.

These tests never call a real model: a fake provider stands in for the engine.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from onion_harness.core import EventType, Task, TaskKind
from onion_harness.harness.bridge import EngineMessage, EngineToolCall, EngineToolResult
from onion_harness.harness.context import ContextCompressor, estimate_tokens
from onion_harness.harness.errors import BoundaryError, friendly_provider_error, sanitize
from onion_harness.harness.meta import MetaTools
from onion_harness.harness.presets import (
    PRESET_BY_ID,
    THINKING_BUDGET,
    all_presets,
    preset_for,
)
from onion_harness.harness.provider import AnthropicProvider
from onion_harness.harness.runtime import build_model_config
from onion_harness.ledger import ChangeKind, ChangeLedger, LedgerStore


# --------------------------------------------------------------- compression
class CompressionTest(unittest.TestCase):
    def test_short_history_is_untouched(self):
        messages = [EngineMessage.user(text=f"m{i}") for i in range(4)]
        out, stats = ContextCompressor(keep_recent=8).compress(messages)
        self.assertEqual(len(out), 4)
        self.assertFalse(stats["compressed"])

    def test_keeps_recent_and_summarises_older(self):
        messages = [EngineMessage.system(text="system rules")] + [
            EngineMessage.user(text=f"turn {i}") for i in range(20)
        ]
        out, stats = ContextCompressor(keep_recent=6).compress(messages)
        self.assertTrue(stats["compressed"])
        self.assertGreater(stats["dropped_messages"], 0)
        self.assertLess(len(out), len(messages))
        # Last six user turns survive verbatim.
        tail_texts = [m.text for m in out if m.type == "user"]
        for i in range(14, 20):
            self.assertIn(f"turn {i}", tail_texts)

    def test_summary_is_delivered_as_system_message(self):
        messages = [EngineMessage.user(text=f"turn {i}") for i in range(20)]
        out, stats = ContextCompressor(keep_recent=5).compress(messages)
        system_messages = [m for m in out if m.type == "system"]
        self.assertEqual(len(system_messages), 1)
        self.assertIn("compressed earlier context", system_messages[0].text)
        self.assertTrue(stats["summary"])

    def test_original_system_prompt_is_preserved(self):
        messages = [EngineMessage.system(text="CRITICAL RULES")] + [
            EngineMessage.user(text=f"turn {i}") for i in range(20)
        ]
        out, _ = ContextCompressor(keep_recent=4).compress(messages)
        system_texts = [m.text for m in out if m.type == "system"]
        self.assertTrue(any("CRITICAL RULES" in text for text in system_texts))

    def test_summary_records_tools_and_errors(self):
        messages = [
            EngineMessage.user(text="make a report"),
            EngineMessage.assistant(
                text="running",
                tool_calls=[EngineToolCall(id="1", name="run_python", arguments={})],
            ),
            EngineMessage.user(
                tool_results=[
                    EngineToolResult(tool_use_id="1", name="run_python", content="Traceback: boom", is_error=True)
                ]
            ),
        ] + [EngineMessage.user(text=f"filler {i}") for i in range(10)]
        out, stats = ContextCompressor(keep_recent=3).compress(messages)
        self.assertIn("run_python", stats["summary"])
        self.assertIn("error", stats["summary"].lower())

    def test_token_estimate_is_positive(self):
        self.assertGreater(estimate_tokens("hello world"), 0)
        self.assertEqual(estimate_tokens(""), 0)

    def test_compression_is_deterministic(self):
        def build():
            return [EngineMessage.user(text=f"turn {i}") for i in range(16)]

        a = ContextCompressor(keep_recent=4).compress(build())[1]["summary"]
        b = ContextCompressor(keep_recent=4).compress(build())[1]["summary"]
        self.assertEqual(a, b)


# ------------------------------------------------------------------- presets
class PresetTest(unittest.TestCase):
    def test_deepseek_prefills_official_base_url(self):
        preset = preset_for("deepseek")
        self.assertIsNotNone(preset)
        self.assertEqual(preset.base_url, "https://api.deepseek.com/v1")
        self.assertIn("deepseek-chat", preset.models)

    def test_all_presets_have_icons_and_formats(self):
        for preset in all_presets():
            self.assertIn(preset["format"], {"openai", "anthropic"})
            self.assertTrue(preset["icon"])

    def test_anthropic_uses_anthropic_format(self):
        self.assertEqual(PRESET_BY_ID["anthropic"].format, "anthropic")

    def test_custom_providers_can_be_added(self):
        extra = [{"label": "My Endpoint", "base_url": "http://10.0.0.5:8000/v1"}]
        presets = all_presets(extra)
        self.assertTrue(any(p["label"] == "My Endpoint" and not p["builtin"] for p in presets))

    def test_thinking_budgets_increase(self):
        self.assertEqual(THINKING_BUDGET["off"], 0)
        ordered = ["off", "low", "medium", "high", "max"]
        budgets = [THINKING_BUDGET[key] for key in ordered]
        self.assertEqual(budgets, sorted(budgets))


class ModelConfigTest(unittest.TestCase):
    def test_preset_fills_missing_fields(self):
        config = build_model_config({"provider_id": "deepseek", "api_key": "k"})
        self.assertEqual(config.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(config.wire_format, "openai")
        self.assertEqual(config.model, "deepseek-chat")

    def test_explicit_values_win(self):
        config = build_model_config(
            {
                "provider_id": "custom",
                "base_url": "https://my.host/v1",
                "model": "my-model",
                "wire_format": "anthropic",
            }
        )
        self.assertEqual(config.base_url, "https://my.host/v1")
        self.assertEqual(config.wire_format, "anthropic")

    def test_keys_are_resolved_from_the_key_store(self):
        config = build_model_config({"provider_id": "openai", "model": "gpt-4o"}, {"openai": "sk-abc"})
        self.assertEqual(config.api_key, "sk-abc")
        self.assertTrue(config.to_public()["has_api_key"])

    def test_public_payload_never_contains_the_key(self):
        config = build_model_config({"provider_id": "openai", "api_key": "sk-secret"})
        self.assertNotIn("sk-secret", json.dumps(config.to_public()))

    def test_limits_are_clamped(self):
        config = build_model_config({"provider_id": "openai", "max_turns": 999_999})
        self.assertEqual(config.max_turns, 10_000)

    def test_long_runs_are_allowed(self):
        """40 was too low for an autonomous run; 10,000 is the new ceiling."""
        config = build_model_config({"provider_id": "openai", "max_turns": 5000})
        self.assertEqual(config.max_turns, 5000)

    def test_zero_turns_is_raised_to_one(self):
        config = build_model_config({"provider_id": "openai", "max_turns": 0})
        self.assertEqual(config.max_turns, 1)


# ------------------------------------------------------- anthropic translation
class AnthropicAdapterTest(unittest.TestCase):
    def setUp(self):
        self.provider = AnthropicProvider(
            base_url="https://api.anthropic.com/v1", api_key="k", model="claude-test"
        )

    def test_system_messages_are_hoisted(self):
        messages = [
            EngineMessage.system(text="rules one"),
            EngineMessage.system(text="rules two"),
            EngineMessage.user(text="hello"),
        ]
        system, converted = self.provider._to_anthropic_messages(messages)
        self.assertIn("rules one", system)
        self.assertIn("rules two", system)
        self.assertEqual(converted[0]["role"], "user")

    def test_tool_calls_become_tool_use_blocks(self):
        messages = [
            EngineMessage.user(text="do it"),
            EngineMessage.assistant(
                text="working",
                tool_calls=[EngineToolCall(id="t1", name="run_python", arguments={"code": "1"})],
            ),
        ]
        _, converted = self.provider._to_anthropic_messages(messages)
        blocks = converted[1]["content"]
        types = [b["type"] for b in blocks]
        self.assertIn("tool_use", types)
        tool_use = next(b for b in blocks if b["type"] == "tool_use")
        self.assertEqual(tool_use["id"], "t1")
        self.assertEqual(tool_use["input"], {"code": "1"})

    def test_tool_results_become_tool_result_blocks(self):
        messages = [
            EngineMessage.user(text="do it"),
            EngineMessage.assistant(
                tool_calls=[EngineToolCall(id="t1", name="run_python", arguments={})]
            ),
            EngineMessage.user(
                tool_results=[
                    EngineToolResult(tool_use_id="t1", name="run_python", content="ok", is_error=False)
                ]
            ),
        ]
        _, converted = self.provider._to_anthropic_messages(messages)
        last = converted[-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][0]["type"], "tool_result")
        self.assertEqual(last["content"][0]["tool_use_id"], "t1")

    def test_alternating_roles_are_merged(self):
        messages = [
            EngineMessage.user(text="a"),
            EngineMessage.user(tool_results=[EngineToolResult(tool_use_id="x", name="n", content="c")]),
            EngineMessage.user(text="b"),
        ]
        _, converted = self.provider._to_anthropic_messages(messages)
        roles = [m["role"] for m in converted]
        for first, second in zip(roles, roles[1:]):
            self.assertNotEqual(first, second)

    def test_conversation_starts_with_user(self):
        messages = [EngineMessage.assistant(text="orphan")]
        _, converted = self.provider._to_anthropic_messages(messages)
        self.assertEqual(converted[0]["role"], "user")

    def test_response_parsing(self):
        data = {
            "model": "claude-test",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "consider this"},
                {"type": "text", "text": "Let me run it."},
                {"type": "tool_use", "id": "tu1", "name": "run_python", "input": {"code": "print(1)"}},
            ],
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
        response = self.provider._parse_anthropic_response(data)
        self.assertEqual(response.text, "Let me run it.")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "run_python")
        self.assertEqual(response.tool_calls[0].arguments, {"code": "print(1)"})
        self.assertEqual(response.usage.prompt_tokens, 12)
        self.assertEqual(response.usage.total_tokens, 19)
        self.assertIn("consider this", self.provider.last_reasoning)

    def test_tool_schema_passthrough(self):
        class Def:
            name = "run_cmd"
            description = "d"
            input_schema = {"type": "object", "properties": {"code": {"type": "string"}}}

        tool = self.provider._to_anthropic_tool(Def())
        self.assertEqual(tool["name"], "run_cmd")
        self.assertIn("properties", tool["input_schema"])


# -------------------------------------------------------------- error boundary
class ErrorBoundaryTest(unittest.TestCase):
    def test_sanitize_removes_engine_naming(self):
        token = "loong" + "_agent"
        leaky = f"Traceback in {token}.loop.engine: LoongAgentOptions failed"
        cleaned = sanitize(leaky)
        self.assertNotIn(token, cleaned)
        self.assertNotIn("LoongAgent", cleaned)
        self.assertIn("onion", cleaned.lower())

    def test_sanitize_handles_hyphenated_name(self):
        token = "loong" + "-agent"
        self.assertNotIn(token, sanitize(f"pip install {token}"))

    def test_auth_error_is_friendly(self):
        class Response:
            status_code = 401
            text = '{"error": {"message": "Invalid API key"}}'

        class Exc(Exception):
            response = Response()

        friendly = friendly_provider_error(Exc())
        self.assertEqual(friendly.code, "auth")
        self.assertIn("API key", friendly.public_message)

    def test_rate_limit(self):
        class Response:
            status_code = 429
            text = ""

        class Exc(Exception):
            response = Response()

        self.assertEqual(friendly_provider_error(Exc()).code, "rate-limit")

    def test_network_error(self):
        class ConnectError(Exception):
            pass

        self.assertEqual(friendly_provider_error(ConnectError("refused")).code, "network")

    def test_detail_is_sanitized(self):
        token = "loong" + "_agent"
        err = BoundaryError("failed", detail=f"{token}.llm.openai_compatible")
        self.assertNotIn(token, err.detail)


# --------------------------------------------------------------- meta tools
class MetaToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-meta-"))
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.meta = MetaTools(self.ledger, "task-1")

    def tearDown(self):
        self.ledger.close()

    def by_name(self, name):
        return next(tool for tool in self.meta.tools() if tool.name == name)

    def test_all_meta_operations_exist(self):
        names = {tool.name for tool in self.meta.tools()}
        self.assertEqual(
            names,
            {
                "onion_register_executor",
                "onion_register_model_provider",
                "onion_update_system_prompt",
                "onion_update_ui_panel",
                "onion_update_skill_registry",
                # v2: Create MCP
                "onion_create_mcp_tool",
                "onion_remove_mcp_tool",
            },
        )

    def test_mcp_tool_rejects_unsafe_code(self):
        tool = self.by_name("onion_create_mcp_tool")
        message = tool.handler(
            {
                "name": "sneaky",
                "description": "reads the network",
                "parameters": {"type": "object", "properties": {}},
                "code": "import socket\ndef handle(a):\n    return 1\n",
                "reason": "test",
            }
        )
        self.assertIn("rejected", message.lower())
        self.assertEqual(self.ledger.list_proposals(), [])

    def test_mcp_tool_proposal_is_staged_only(self):
        tool = self.by_name("onion_create_mcp_tool")
        message = tool.handler(
            {
                "name": "word_count",
                "description": "count words",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
                "code": "def handle(a):\n    return len(str(a.get('text','')).split())\n",
                "reason": "useful",
            }
        )
        self.assertIn("NOT applied", message)
        self.assertEqual(len(self.ledger.list_proposals()), 1)
        self.assertEqual(self.ledger.state.get("mcp_tools"), [])

    def test_no_tool_can_apply(self):
        for tool in self.meta.tools():
            self.assertNotIn("apply", tool.name)
            self.assertNotIn("commit", tool.name)
            self.assertNotIn("confirm", tool.name)

    def test_proposal_tool_only_stages(self):
        tool = self.by_name("onion_update_system_prompt")
        before = self.ledger.state["system_prompt_segments"][0]["content"]
        message = tool.handler({"segment_id": "core", "content": "new", "reason": "test"})
        self.assertIn("NOT applied", message)
        self.assertEqual(self.ledger.state["system_prompt_segments"][0]["content"], before)
        self.assertEqual(len(self.ledger.list_proposals()), 1)

    def test_validation_failures_are_reported_not_raised(self):
        tool = self.by_name("onion_register_executor")
        message = tool.handler({"name": "not-a-real-runtime", "reason": "test"})
        self.assertIn("rejected", message.lower())
        self.assertEqual(self.ledger.list_proposals(), [])

    def test_register_provider_requires_http_url(self):
        tool = self.by_name("onion_register_model_provider")
        message = tool.handler({"label": "X", "base_url": "file:///etc/passwd", "reason": "x"})
        self.assertIn("rejected", message.lower())

    def test_meta_tools_are_not_exposed_as_apply_capability(self):
        """The tool surface must be incapable of confirming anything."""
        for tool in self.meta.tools():
            schema = json.dumps(tool.input_schema)
            self.assertNotIn("accept_irreversible", schema)
            self.assertNotIn("confirm", schema)


# ------------------------------------------------------------ runtime wrapper
class _FakeProvider:
    """Minimal provider double used to exercise the runtime without a network."""

    def __init__(self, script):
        self.model = "fake-model"
        self.last_reasoning = ""
        self._script = list(script)
        self._calls = 0

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        from onion_harness.harness.bridge import EngineChatResponse, EngineUsage

        self._calls += 1
        step = self._script[min(self._calls - 1, len(self._script) - 1)]
        if "reasoning" in step:
            self.last_reasoning = step["reasoning"]
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=self.model,
        )


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        from onion_harness.harness import runtime as runtime_module

        self.runtime_module = runtime_module
        self.dir = Path(tempfile.mkdtemp(prefix="onion-rt-"))
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        from onion_harness.plugins import builtin_executors
        from onion_harness.sandbox import Sandbox

        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self._original = runtime_module.build_provider

    def tearDown(self):
        self.runtime_module.build_provider = self._original
        self.ledger.close()

    def _run(self, script, prompt="do it", api_key="sk-test"):
        self.runtime_module.build_provider = lambda *a, **k: _FakeProvider(script)
        from onion_harness.harness.runtime import OnionRuntime

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )
        model = build_model_config(
            {"provider_id": "deepseek", "model": "deepseek-chat", "api_key": api_key}
        )
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt=prompt, workspace=str(self.dir)), model):
                events.append(ev)

        asyncio.run(consume())
        return events

    def test_event_stream_shape(self):
        events = self._run([{"text": "all done"}])
        types = [ev.type for ev in events]
        self.assertEqual(types[0], EventType.TASK_CREATED)
        self.assertEqual(types[-1], EventType.TASK_DONE)
        self.assertIn(EventType.ASSISTANT, types)
        self.assertIn(EventType.TOKEN_USAGE, types)
        # Sequence numbers are strictly increasing.
        seqs = [ev.seq for ev in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_tool_call_and_result_are_paired(self):
        script = [
            {
                "tool_calls": [EngineToolCall(id="c1", name="run_python", arguments={"code": "print('42')"})]
            },
            {"text": "finished"},
        ]
        events = self._run(script)
        calls = [e for e in events if e.type == EventType.TOOL_CALL]
        results = [e for e in events if e.type == EventType.TOOL_RESULT]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(calls[0].payload["call_id"], results[0].payload["call_id"])
        self.assertEqual(calls[0].payload["executor"], "python")
        self.assertEqual(results[0].payload["status"], "success")
        self.assertIn("42", results[0].payload["content"])

    def test_reasoning_is_streamed_as_collapsed_card(self):
        events = self._run([{"reasoning": "thinking hard", "text": "answer"}])
        reasoning = [e for e in events if e.type == EventType.REASONING]
        self.assertTrue(reasoning)
        self.assertEqual(reasoning[0].payload["text"], "thinking hard")
        self.assertTrue(reasoning[0].payload["collapsed"])

    def test_missing_api_key_fails_cleanly(self):
        events = self._run([{"text": "never runs"}], api_key="")
        errors = [e for e in events if e.type == EventType.ERROR]
        self.assertTrue(errors)
        self.assertIn("API key", errors[0].payload["message"])
        # No assistant turn should have been produced.
        self.assertFalse([e for e in events if e.type == EventType.ASSISTANT])

    def test_error_messages_never_leak_engine_names(self):
        token = "loong" + "_agent"

        class Boom(Exception):
            pass

        self.runtime_module.build_provider = lambda *a, **k: (_ for _ in ()).throw(Boom(f"{token}.boom"))
        from onion_harness.harness.runtime import OnionRuntime

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )
        model = build_model_config({"provider_id": "deepseek", "api_key": "k"})
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt="x"), model):
                events.append(ev)

        asyncio.run(consume())
        dump = json.dumps([e.to_dict() for e in events])
        self.assertNotIn(token, dump)

    def test_system_prompt_contains_executor_hints(self):
        from onion_harness.harness.runtime import OnionRuntime

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )
        prompt = runtime._compose_system_prompt(self.ledger.state)
        self.assertIn("run_python", prompt)
        self.assertIn("run_powershell", prompt)
        self.assertIn("onion_", prompt)

    def test_prompt_reflects_applied_meta_changes(self):
        from onion_harness.harness.runtime import OnionRuntime
        from onion_harness.ledger import ChangeRecord

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )
        marker = "UNIQUE_PROMPT_MARKER_42"
        change_id = self.ledger.propose(
            ChangeRecord(kind=ChangeKind.UPDATE_SYSTEM_PROMPT, params={"content": marker})
        )
        self.ledger.apply(change_id)
        self.assertIn(marker, runtime._compose_system_prompt(self.ledger.state))

    def test_cancellation_flag_stops_the_run(self):
        import threading

        self.runtime_module.build_provider = lambda *a, **k: _FakeProvider([{"text": "x"}])
        from onion_harness.harness.runtime import OnionRuntime

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )
        canceller = threading.Event()
        canceller.set()
        model = build_model_config({"provider_id": "deepseek", "api_key": "k"})
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt="x"), model, cancel_event=canceller):
                events.append(ev)

        asyncio.run(consume())
        self.assertEqual(events[-1].type, EventType.TASK_DONE)
        self.assertEqual(events[-1].payload["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

class TurnLimitMessageTest(unittest.TestCase):
    """The engine reports the limit as a bare error; it must read as actionable."""

    def test_the_message_says_it_is_a_limit_not_a_failure(self):
        from onion_harness.harness.adapters import _explain_turn_limit

        text = _explain_turn_limit("Stopped after reaching max_turns=40", 40)
        self.assertIn("turn budget", text)
        self.assertIn("40", text)
        self.assertIn("Settings", text)
        self.assertIn("not a failure", text)

    def test_other_errors_pass_through_unchanged(self):
        from onion_harness.harness.adapters import _explain_turn_limit

        self.assertEqual(_explain_turn_limit("connection refused", 40), "connection refused")

    def test_unknown_limit_still_explains(self):
        from onion_harness.harness.adapters import _explain_turn_limit

        text = _explain_turn_limit("Stopped after reaching max_turns=40", 0)
        self.assertIn("Settings", text)
        self.assertNotIn("currently", text)
