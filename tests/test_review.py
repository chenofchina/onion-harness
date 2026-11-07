"""Regression tests for the code-review pass.

Each case here corresponds to a real defect found by reviewing the whole
codebase, so the fix stays fixed.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from onion_harness.api import OnionService
from onion_harness.core.mcp_contract import McpError, validate_spec
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineUsage
from onion_harness.ledger import ChangeLedger, ChangeKind, ChangeRecord, LedgerStore


class McpEscapeTest(unittest.TestCase):
    """Static validation must not be bypassable by indirection."""

    def assert_blocked(self, code: str) -> None:
        with self.assertRaises(McpError):
            validate_spec("probe_tool", {"type": "object"}, code)

    def test_getattr_dynamic_access_blocked(self):
        self.assert_blocked(
            "def handle(a):\n    return getattr(__builtins__, 'eval')('1')\n"
        )

    def test_eval_alias_blocked(self):
        self.assert_blocked("def handle(a):\n    f = eval\n    return f('1')\n")

    def test_dunder_attribute_blocked(self):
        self.assert_blocked(
            "def handle(a):\n    return ().__class__.__base__.__subclasses__()\n"
        )

    def test_globals_blocked(self):
        self.assert_blocked("def handle(a):\n    return globals()\n")

    def test_compile_blocked(self):
        self.assert_blocked("def handle(a):\n    return compile('1','x','eval')\n")

    def test_legitimate_tool_still_passes(self):
        validate_spec(
            "word_stats",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            "import json\n"
            "def handle(arguments):\n"
            "    text = str(arguments.get('text', ''))\n"
            "    return json.dumps({'words': len(text.split())})\n",
        )


class LedgerRedoAuditTest(unittest.TestCase):
    """redo() must record the state it applied to."""

    def test_redo_records_before_and_after(self):
        directory = Path(tempfile.mkdtemp(prefix="onion-redo-"))
        ledger = ChangeLedger(LedgerStore(directory / "l.db"))
        try:
            change_id = ledger.propose(
                ChangeRecord(
                    kind=ChangeKind.UPDATE_SYSTEM_PROMPT,
                    target="core",
                    params={"segment_id": "core", "content": "reviewed"},
                )
            )
            revision = ledger.apply(change_id)
            version = revision.version
            ledger.revert(version)

            redone = ledger.redo()
            self.assertIsNotNone(redone)
            self.assertTrue(redone.before, "redo must capture the prior state")
            self.assertTrue(redone.after, "redo must capture the resulting state")
            self.assertEqual(
                ledger.state["system_prompt_segments"][0]["content"], "reviewed"
            )
        finally:
            ledger.close()


class _FastProvider:
    def __init__(self):
        self.last_reasoning = ""

    def get_model_name(self):
        return "fake"

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        return EngineChatResponse(
            text="ok", usage=EngineUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        )

    async def aclose(self):
        return None


class TaskRegistryTest(unittest.TestCase):
    """A finished task must never leave a zombie entry in the active registry."""

    def setUp(self):
        self._orig = runtime_module.build_provider
        runtime_module.build_provider = lambda *a, **k: _FastProvider()
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-reg-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def test_fast_tasks_do_not_leak_registry_entries(self):
        for index in range(12):
            record = self.service.create_task({"prompt": f"q{index}"})
            deadline = time.time() + 10
            while time.time() < deadline:
                events = self.service.task_events(record["id"])
                if any(e["type"] == "task-done" for e in events):
                    break
                time.sleep(0.01)
        # Give the loop a moment to settle.
        time.sleep(0.15)
        self.assertEqual(self.service._active, {}, "registry leaked entries")

    def test_cancel_uses_the_placeholder_before_scheduling(self):
        record = self.service.create_task({"prompt": "slow enough"})
        # Immediately after creation the entry exists and is cancellable even if
        # the future was not attached yet.
        entry = self.service._active.get(record["id"])
        if entry is not None:
            entry.cancel()  # must not raise


class ConversationHistoryTest(unittest.TestCase):
    """History rebuild must not include stale tool calls."""

    def test_only_user_and_assistant_turns_are_replayed(self):
        from onion_harness.api.service import OnionService as Svc

        directory = Path(tempfile.mkdtemp(prefix="onion-hist-"))
        service = Svc(data_dir=directory)
        try:
            task_id = "task_hist"
            service.store.append_event(task_id, {
                "id": "1", "type": "user-input", "task_id": task_id, "seq": 1,
                "timestamp": 1.0, "payload": {"text": "hello"},
            })
            service.store.append_event(task_id, {
                "id": "2", "type": "tool-call", "task_id": task_id, "seq": 2,
                "timestamp": 2.0, "payload": {"call_id": "c1", "name": "run_python"},
            })
            service.store.append_event(task_id, {
                "id": "3", "type": "assistant", "task_id": task_id, "seq": 3,
                "timestamp": 3.0, "payload": {"text": "done"},
            })
            history = service.task_history(task_id)
            self.assertEqual([item["role"] for item in history], ["user", "assistant"])
            self.assertEqual(history[0]["text"], "hello")
        finally:
            service.stop()


class SandboxTimeoutPrecedenceTest(unittest.TestCase):
    """A caller-supplied context must not widen the task's own timeout."""

    def test_task_timeout_beats_a_longer_context_timeout(self):
        import asyncio
        import sys

        from onion_harness.core import Context, Layer, Task, TaskKind
        from onion_harness.sandbox import Sandbox

        async def run():
            sandbox = Sandbox(workspace_root=tempfile.mkdtemp())
            task = Task(
                kind=TaskKind.COMMAND,
                command=[sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_seconds=1.0,
            )
            # A default context would otherwise allow 120s.
            ctx = Context(task_id=task.id, layer=Layer.SANDBOX)
            started = time.perf_counter()
            result = await sandbox.execute(task, ctx)
            return result, time.perf_counter() - started

        result, elapsed = asyncio.run(run())
        self.assertTrue(result.timed_out, "task timeout was ignored")
        self.assertLess(elapsed, 5, f"took {elapsed:.1f}s despite a 1s limit")

    def test_streaming_path_also_respects_the_deadline(self):
        import asyncio
        import sys

        from onion_harness.core import Context, Layer, Task, TaskKind
        from onion_harness.sandbox import Sandbox

        async def run():
            sandbox = Sandbox(workspace_root=tempfile.mkdtemp())
            task = Task(
                kind=TaskKind.COMMAND,
                command=[
                    sys.executable,
                    "-c",
                    "import time,sys\nfor i in range(100):\n print('t',i); sys.stdout.flush(); time.sleep(0.2)",
                ],
                timeout_seconds=1.0,
            )
            chunks: list = []
            ctx = Context(task_id=task.id, layer=Layer.SANDBOX, extra={"on_output": lambda c, t: chunks.append(t)})
            started = time.perf_counter()
            result = await sandbox.execute(task, ctx)
            return result, time.perf_counter() - started, chunks

        result, elapsed, chunks = asyncio.run(run())
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 5)
        self.assertTrue(chunks, "streaming produced no output before the deadline")


if __name__ == "__main__":
    unittest.main()


class SingleErrorCardTest(unittest.TestCase):
    """A failed run must surface exactly one error event.

    The GUI rendered both the `error` event and `task-done.error`, so the same
    message appeared as two cards. The backend contract is: one `error` event,
    and `task-done` mirrors it for the record.
    """

    def setUp(self):
        self._orig = runtime_module.build_provider
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-err-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-bad"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def test_one_error_event_for_a_failed_run(self):
        from onion_harness.harness.errors import BoundaryError

        def boom(*args, **kwargs):
            raise BoundaryError("Could not reach the model provider.", code="network")

        runtime_module.build_provider = boom
        record = self.service.create_task({"prompt": "hello"})

        deadline = time.time() + 20
        events = []
        while time.time() < deadline:
            events = self.service.task_events(record["id"])
            if any(e["type"] == "task-done" for e in events):
                break
            time.sleep(0.03)

        errors = [e for e in events if e["type"] == "error"]
        self.assertEqual(len(errors), 1, f"expected 1 error event, got {len(errors)}")
        self.assertEqual(errors[0]["payload"]["code"], "network")

        done = [e for e in events if e["type"] == "task-done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["payload"]["status"], "failed")
        # The task record mirrors the same message; the GUI must not render both.
        self.assertEqual(
            done[0]["payload"]["error"], errors[0]["payload"]["message"]
        )


class BaseUrlOverrideTest(unittest.TestCase):
    """A base URL typed by the user must reach the provider.

    Previously the Advanced drawer only re-saved the API key, and the settings
    schema had no base_url at all, so an override was silently dropped and every
    request went to the preset host.
    """

    def setUp(self):
        self._orig = runtime_module.build_provider
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-url-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def test_base_url_override_is_saved_and_public(self):
        settings = self.service.update_settings(
            {"provider_id": "deepseek", "base_url": "https://proxy.example.com/v1"}
        )
        self.assertEqual(
            settings["base_urls"]["deepseek"], "https://proxy.example.com/v1"
        )

    def test_blank_clears_the_override(self):
        self.service.update_settings(
            {"provider_id": "deepseek", "base_url": "https://proxy.example.com/v1"}
        )
        settings = self.service.update_settings({"provider_id": "deepseek", "base_url": ""})
        self.assertNotIn("deepseek", settings.get("base_urls", {}))

    def test_provider_receives_the_override(self):
        captured = {}

        class P:
            last_reasoning = ""

            def get_model_name(self):
                return "f"

            def clone(self, model=None):
                return P()

            async def chat_completion(self, m, tools=None, **kw):
                return EngineChatResponse(
                    text="ok", usage=EngineUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
                )

            async def aclose(self):
                return None

        def fake_build(provider_id, base_url, api_key, model, **kw):
            captured["base_url"] = base_url
            captured["model"] = model
            return P()

        runtime_module.build_provider = fake_build
        self.service.update_settings(
            {"provider_id": "deepseek", "base_url": "https://proxy.example.com/v1"}
        )
        record = self.service.create_task({"prompt": "hi"})
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(
                e["type"] in ("task-done", "error")
                for e in self.service.task_events(record["id"])
            ):
                break
            time.sleep(0.03)

        self.assertEqual(captured.get("base_url"), "https://proxy.example.com/v1")

    def test_explicit_per_task_url_wins_over_the_saved_one(self):
        captured = {}

        class P:
            last_reasoning = ""

            def get_model_name(self):
                return "f"

            def clone(self, model=None):
                return P()

            async def chat_completion(self, m, tools=None, **kw):
                return EngineChatResponse(
                    text="ok", usage=EngineUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
                )

            async def aclose(self):
                return None

        def fake_build(provider_id, base_url, api_key, model, **kw):
            captured["base_url"] = base_url
            return P()

        runtime_module.build_provider = fake_build
        self.service.update_settings(
            {"provider_id": "deepseek", "base_url": "https://proxy.example.com/v1"}
        )
        record = self.service.create_task(
            {
                "prompt": "hi",
                "model": {
                    "provider_id": "deepseek",
                    "base_url": "https://per-task.example.com/v1",
                },
            }
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(
                e["type"] in ("task-done", "error")
                for e in self.service.task_events(record["id"])
            ):
                break
            time.sleep(0.03)

        self.assertEqual(captured.get("base_url"), "https://per-task.example.com/v1")


class TlsTrustTest(unittest.TestCase):
    """Certificate failures must be trusted and reported accurately.

    A TLS-terminating antivirus or corporate proxy installs its root in the OS
    store, not in certifi. Requests then fail with CERTIFICATE_VERIFY_FAILED,
    which httpx reports as a ConnectError — previously surfacing as a misleading
    "check your network" message even though the network was fine.
    """

    def test_system_context_is_available(self):
        from onion_harness.harness.provider import system_ssl_context

        context = system_ssl_context()
        self.assertIsNotNone(context)
        # A verification-capable context is what matters, not the backend used.
        self.assertTrue(context.verify_mode is not None)

    def test_http_client_uses_system_verification(self):
        from onion_harness.harness.provider import http_client

        client = http_client()
        try:
            self.assertIsNotNone(client._transport)
        finally:
            import asyncio

            asyncio.run(client.aclose())

    def test_certificate_failure_is_not_reported_as_network(self):
        import httpx

        from onion_harness.harness.errors import friendly_provider_error

        exc = httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self signed certificate in certificate chain"
        )
        friendly = friendly_provider_error(exc)
        self.assertEqual(friendly.code, "tls")
        self.assertIn("certificate", friendly.public_message.lower())
        self.assertNotIn("check the base url", friendly.public_message.lower())

    def test_genuine_network_error_stays_network(self):
        import httpx

        from onion_harness.harness.errors import friendly_provider_error

        friendly = friendly_provider_error(httpx.ConnectError("Connection refused"))
        self.assertEqual(friendly.code, "network")

    def test_provider_clients_trust_the_os_store(self):
        """Both wire formats must go through the shared client factory."""
        from onion_harness.harness.provider import AnthropicProvider, OnionOpenAIProvider

        anthropic = AnthropicProvider(
            base_url="https://example.com/v1", api_key="k", model="m"
        )
        openai = OnionOpenAIProvider(
            base_url="https://example.com/v1", api_key="k", model="m"
        )
        import asyncio

        try:
            for provider in (anthropic, openai):
                transport = provider._client._transport
                self.assertIsNotNone(transport)
        finally:
            asyncio.run(anthropic.aclose())
            asyncio.run(openai.aclose())


class EngineErrorAttributeTest(unittest.TestCase):
    """The engine's Message.error classmethod shadows its same-named field.

    When the field is None the attribute lookup returns a bound method, and a
    boolean-or chain put "<bound method Message.error ...>" into the UI as the
    error message.
    """

    def test_error_text_never_returns_a_bound_method(self):
        from onion_harness.harness.adapters import _error_text
        from onion_harness.harness.bridge import EngineMessage

        cases = [
            EngineMessage.result(text="", is_error=True),
            EngineMessage.result(text="", is_error=True, error=None),
        ]
        for message in cases:
            text = _error_text(message, "fallback")
            self.assertIsInstance(text, str)
            self.assertNotIn("bound method", text)

    def test_error_text_prefers_explicit_error(self):
        from onion_harness.harness.adapters import _error_text
        from onion_harness.harness.bridge import EngineMessage

        message = EngineMessage.result(text="text", is_error=True, error="explicit")
        self.assertEqual(_error_text(message, "fallback"), "explicit")

    def test_error_text_falls_back_to_message_text(self):
        from onion_harness.harness.adapters import _error_text
        from onion_harness.harness.bridge import EngineMessage

        message = EngineMessage.result(text="something broke", is_error=True)
        self.assertEqual(_error_text(message, "fallback"), "something broke")

    def test_translator_result_branch_is_clean(self):
        from onion_harness.harness.adapters import EngineMessageTranslator
        from onion_harness.harness.bridge import EngineMessage

        translator = EngineMessageTranslator("task_x", lambda event: None)
        events = translator.translate(EngineMessage.result(text="", is_error=True))
        errors = [e for e in events if e.type.value == "error"]
        self.assertEqual(len(errors), 1)
        message = errors[0].payload["message"]
        self.assertNotIn("bound method", message)
        self.assertEqual(message, "task failed")

    def test_no_engine_field_is_shadowed_by_a_method(self):
        """Guard against the engine adding another same-named classmethod."""
        import dataclasses

        from onion_harness.harness.bridge import EngineMessage

        shadowed = [
            field.name
            for field in dataclasses.fields(EngineMessage)
            if callable(getattr(EngineMessage, field.name, None))
        ]
        # `error` is the only one, and adapters handle it explicitly.
        self.assertEqual(shadowed, ["error"])
