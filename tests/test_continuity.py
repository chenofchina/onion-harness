"""Conversation continuity: a follow-up continues the same thread."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from onion_harness.api import OnionService
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineUsage
from onion_harness.harness.provider import HistoryReplayProvider

SEEN: list = []


class _Recorder:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_reasoning = ""

    def get_model_name(self):
        return "fake"

    def clone(self, model=None):
        return _Recorder(self._reply)

    async def chat_completion(self, messages, tools=None, **kwargs):
        SEEN.append(
            [(getattr(m, "type", "?"), (getattr(m, "text", "") or "")) for m in messages]
        )
        return EngineChatResponse(
            text=self._reply,
            usage=EngineUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def aclose(self):
        return None


class HistoryReplayProviderTest(unittest.TestCase):
    def test_history_is_inserted_after_system_messages(self):
        from onion_harness.harness.bridge import EngineMessage
        from onion_harness.harness.context import ContextCompressor
        from onion_harness.harness.provider import CompressingProvider

        inner = CompressingProvider(_Recorder("ok"), ContextCompressor())
        provider = HistoryReplayProvider(
            inner,
            [{"role": "user", "text": "first"}, {"role": "assistant", "text": "answer"}],
        )
        import asyncio

        asyncio.run(
            provider.chat_completion(
                [EngineMessage.system(text="SYS"), EngineMessage.user(text="second")]
            )
        )
        roles = [role for role, _ in SEEN[-1]]
        texts = [text for _, text in SEEN[-1]]
        self.assertEqual(roles[0], "system")
        self.assertEqual(texts[0], "SYS")
        self.assertIn("first", texts)
        self.assertIn("answer", texts)
        self.assertIn("second", texts)

    def test_empty_history_is_a_passthrough(self):
        from onion_harness.harness.context import ContextCompressor
        from onion_harness.harness.provider import CompressingProvider
        from onion_harness.harness.bridge import EngineMessage
        import asyncio

        provider = HistoryReplayProvider(CompressingProvider(_Recorder("ok"), ContextCompressor()))
        asyncio.run(provider.chat_completion([EngineMessage.user(text="only")]))
        self.assertEqual(len(SEEN[-1]), 1)

    def test_wrapper_exposes_compression_telemetry(self):
        """The harness reads last_compression off the outermost provider."""
        from onion_harness.harness.context import ContextCompressor
        from onion_harness.harness.provider import CompressingProvider

        provider = HistoryReplayProvider(
            CompressingProvider(_Recorder("ok"), ContextCompressor())
        )
        self.assertIsInstance(provider.last_compression, dict)
        self.assertEqual(provider.last_context_tokens, 0)


class ContinuationServiceTest(unittest.TestCase):
    def setUp(self):
        self._orig = runtime_module.build_provider
        SEEN.clear()
        self.dir = Path(tempfile.mkdtemp(prefix="onion-cont-"))
        self.service = OnionService(data_dir=self.dir)
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def _install(self, replies):
        replies = list(replies)
        state = {"i": 0}

        def factory(*args, **kwargs):
            reply = replies[min(state["i"], len(replies) - 1)]
            state["i"] += 1
            return _Recorder(reply)

        runtime_module.build_provider = factory

    def _wait(self, task_id, done_count=1, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.service.task_events(task_id)
            if sum(1 for e in events if e["type"] == "task-done") >= done_count:
                return events
            time.sleep(0.03)
        raise AssertionError("task never finished")

    def test_user_prompt_is_persisted(self):
        """Without this, rebuilding history loses every user turn."""
        self._install(["answer"])
        record = self.service.create_task({"prompt": "first question"})
        events = self._wait(record["id"])
        users = [e for e in events if e["type"] == "user-input"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["payload"]["text"], "first question")

    def test_follow_up_carries_prior_turns(self):
        self._install(["First answer.", "Second answer."])
        record = self.service.create_task({"prompt": "What is 2+2?"})
        self._wait(record["id"])

        self.service.continue_task(record["id"], "And 3+3?")
        events = self._wait(record["id"], done_count=2)

        texts = " ".join(text for _, text in SEEN[-1])
        self.assertIn("What is 2+2?", texts)
        self.assertIn("First answer.", texts)
        self.assertIn("And 3+3?", texts)
        self.assertEqual([r for r, _ in SEEN[-1]][0], "system")

        # Same conversation, both user turns recorded.
        self.assertTrue(all(e["task_id"] == record["id"] for e in events))
        self.assertEqual(sum(1 for e in events if e["type"] == "user-input"), 2)

    def test_follow_up_reuses_the_task_record(self):
        self._install(["a", "b"])
        record = self.service.create_task({"prompt": "one"})
        self._wait(record["id"])
        self.service.continue_task(record["id"], "two")
        self._wait(record["id"], done_count=2)

        tasks = self.service.store.load_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], record["id"])
        self.assertEqual(tasks[0]["turn_count"], 2)

    def test_empty_follow_up_rejected(self):
        from onion_harness.api.service import ApiError

        self._install(["a"])
        record = self.service.create_task({"prompt": "one"})
        self._wait(record["id"])
        with self.assertRaises(ApiError):
            self.service.continue_task(record["id"], "   ")

    def test_unknown_task_rejected(self):
        from onion_harness.api.service import ApiError

        with self.assertRaises(ApiError):
            self.service.continue_task("task_missing", "hello")

    def test_context_window_is_configurable_up_to_one_million(self):
        self._install(["a"])
        record = self.service.create_task({"prompt": "one"})
        self._wait(record["id"])

        updated = self.service.update_task_context(
            record["id"], {"context_window": 1_000_000, "keep_recent": 8}
        )
        self.assertEqual(updated["context_window"], 1_000_000)
        self.assertEqual(updated["keep_recent"], 8)

        clamped = self.service.update_task_context(record["id"], {"context_window": 9_999_999})
        self.assertEqual(clamped["context_window"], 1_000_000)

        floored = self.service.update_task_context(record["id"], {"context_window": 10})
        self.assertEqual(floored["context_window"], 4_000)

    def test_follow_up_uses_overridden_context(self):
        self._install(["a", "b"])
        record = self.service.create_task({"prompt": "one"})
        self._wait(record["id"])
        self.service.continue_task(
            record["id"], "two", context={"context_window": 500_000, "keep_recent": 4}
        )
        self._wait(record["id"], done_count=2)
        task = self.service.store.load_tasks()[0]
        self.assertEqual(task["context_window"], 500_000)
        self.assertEqual(task["keep_recent"], 4)


if __name__ == "__main__":
    unittest.main()


class SettingsBoundsTest(unittest.TestCase):
    """Context settings must be clamped identically at every entry point."""

    def setUp(self):
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-set-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()

    def test_default_context_window(self):
        self.assertEqual(self.service.get_settings()["context_window"], 128_000)

    def test_context_window_accepts_one_million(self):
        updated = self.service.update_settings({"context_window": 1_000_000})
        self.assertEqual(updated["context_window"], 1_000_000)

    def test_context_window_ceiling(self):
        updated = self.service.update_settings({"context_window": 50_000_000})
        self.assertEqual(updated["context_window"], 1_000_000)

    def test_context_window_floor(self):
        updated = self.service.update_settings({"context_window": 10})
        self.assertEqual(updated["context_window"], 4_000)

    def test_non_numeric_context_window_ignored(self):
        before = self.service.get_settings()["context_window"]
        after = self.service.update_settings({"context_window": "not a number"})
        self.assertEqual(after["context_window"], before)

    def test_keep_recent_bounds(self):
        self.assertEqual(
            self.service.update_settings({"keep_recent_messages": 999})["keep_recent_messages"], 40
        )
        self.assertEqual(
            self.service.update_settings({"keep_recent_messages": 1})["keep_recent_messages"], 2
        )

    def test_settings_survive_restart(self):
        self.service.update_settings({"context_window": 512_000, "keep_recent_messages": 24})
        self.service.stop()
        again = OnionService(data_dir=self.service.data_dir)
        settings = again.get_settings()
        self.assertEqual(settings["context_window"], 512_000)
        self.assertEqual(settings["keep_recent_messages"], 24)
        again.stop()
        # Re-open so tearDown still has something to stop.
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-set2-")))
        self.service.start()

    def test_new_tasks_use_the_configured_window(self):
        self._install(["ok"])
        self.service.update_settings({"context_window": 256_000, "keep_recent_messages": 5})
        record = self.service.create_task({"prompt": "hello"})
        self._wait(record["id"])
        task = self.service.store.load_tasks()[0]
        self.assertEqual(task["context_window"], 256_000)
        self.assertEqual(task["keep_recent"], 5)

    def _install(self, replies):
        replies = list(replies)
        state = {"i": 0}

        def factory(*args, **kwargs):
            reply = replies[min(state["i"], len(replies) - 1)]
            state["i"] += 1
            return _Recorder(reply)

        runtime_module.build_provider = factory
        self.addCleanup(lambda: setattr(runtime_module, "build_provider", self._orig))

    def _wait(self, task_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.service.task_events(task_id)
            if any(e["type"] == "task-done" for e in events):
                return events
            time.sleep(0.03)
        raise AssertionError("task never finished")

    def setUp(self):
        self._orig = runtime_module.build_provider
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-set-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()


class NoDuplicateUserBubbleTest(unittest.TestCase):
    """One prompt must produce exactly one user-input event.

    The GUI used to synthesise its own bubble while the server also persisted
    one, so a message could render twice.
    """

    def setUp(self):
        self._orig = runtime_module.build_provider
        runtime_module.build_provider = lambda *a, **k: _Recorder("answer")
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-dup-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def _wait(self, task_id, count=1, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.service.task_events(task_id)
            if sum(1 for e in events if e["type"] == "task-done") >= count:
                return events
            time.sleep(0.03)
        raise AssertionError("task never finished")

    def test_create_returns_the_user_event(self):
        record = self.service.create_task({"prompt": "hello"})
        self.assertIn("user_event", record)
        event = record["user_event"]
        self.assertEqual(event["type"], "user-input")
        self.assertEqual(event["payload"]["text"], "hello")

    def test_only_one_user_event_is_persisted(self):
        record = self.service.create_task({"prompt": "only once"})
        events = self._wait(record["id"])
        users = [e for e in events if e["type"] == "user-input"]
        self.assertEqual(len(users), 1, f"expected 1 user event, got {len(users)}")

    def test_returned_event_matches_the_persisted_one(self):
        record = self.service.create_task({"prompt": "match me"})
        events = self._wait(record["id"])
        persisted = [e for e in events if e["type"] == "user-input"]
        self.assertEqual(record["user_event"]["id"], persisted[0]["id"])

    def test_follow_up_adds_exactly_one_more(self):
        record = self.service.create_task({"prompt": "first"})
        self._wait(record["id"])

        result = self.service.continue_task(record["id"], "second")
        self.assertIn("user_event", result)
        events = self._wait(record["id"], count=2)

        users = [e for e in events if e["type"] == "user-input"]
        self.assertEqual(len(users), 2, f"expected 2 user events, got {len(users)}")
        self.assertEqual([u["payload"]["text"] for u in users], ["first", "second"])

    def test_edit_adds_exactly_one_event(self):
        record = self.service.create_task({"prompt": "original"})
        self._wait(record["id"])

        result = self.service.edit_task(record["id"], "revised")
        self.assertIn("user_event", result)
        events = self._wait(record["id"], count=2)

        users = [e for e in events if e["type"] == "user-input"]
        self.assertEqual(len(users), 2, f"expected 2 user events, got {len(users)}")
        self.assertTrue(users[-1]["payload"].get("edited"))


class SavedContextInheritanceTest(unittest.TestCase):
    """A saved 1M context must actually be used by new conversations.

    Previously the GUI always sent a hardcoded 128k, silently overriding the
    setting the user picked in the Model dialog.
    """

    def setUp(self):
        self._orig = runtime_module.build_provider
        runtime_module.build_provider = lambda *a, **k: _Recorder("ok")
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-ctx-inh-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def _wait(self, task_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(e["type"] == "task-done" for e in self.service.task_events(task_id)):
                return
            time.sleep(0.03)

    def test_new_conversation_inherits_saved_window(self):
        self.service.update_settings({"context_window": 1_000_000, "keep_recent_messages": 24})
        record = self.service.create_task({"prompt": "inherit me"})
        task = next(t for t in self.service.store.load_tasks() if t["id"] == record["id"])
        self.assertEqual(task["context_window"], 1_000_000)
        self.assertEqual(task["keep_recent"], 24)

    def test_explicit_override_still_wins(self):
        self.service.update_settings({"context_window": 1_000_000})
        record = self.service.create_task(
            {
                "prompt": "override",
                "model": {
                    "provider_id": "deepseek",
                    "model": "deepseek-chat",
                    "context_window": 32_000,
                },
            }
        )
        task = next(t for t in self.service.store.load_tasks() if t["id"] == record["id"])
        self.assertEqual(task["context_window"], 32_000)

    def test_follow_up_keeps_the_conversation_window(self):
        self.service.update_settings({"context_window": 512_000})
        record = self.service.create_task({"prompt": "one"})
        self._wait(record["id"])

        # No context payload: the existing conversation value must survive.
        self.service.continue_task(record["id"], "two")
        self._wait(record["id"])
        task = next(t for t in self.service.store.load_tasks() if t["id"] == record["id"])
        self.assertEqual(task["context_window"], 512_000)

    def test_runtime_receives_the_inherited_window(self):
        """The model config, not just the record, must carry the value."""
        from onion_harness.harness.runtime import build_model_config

        self.service.update_settings({"context_window": 1_000_000})
        config = build_model_config(
            {"provider_id": "deepseek", "context_window": self.service.settings["context_window"]}
        )
        self.assertEqual(config.context_window, 1_000_000)


class UsageWindowReportingTest(unittest.TestCase):
    """The usage ring must report the configured window, not a hardcoded 128k."""

    def setUp(self):
        self._orig = runtime_module.build_provider
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-usage-")))
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})
        self.service.update_settings({"context_window": 1_000_000})
        self.service.start()

    def tearDown(self):
        self.service.stop()
        runtime_module.build_provider = self._orig

    def test_token_usage_reports_the_configured_window(self):
        class P:
            def __init__(self):
                self.last_reasoning = ""

            def get_model_name(self):
                return "f"

            def clone(self, model=None):
                return P()

            async def chat_completion(self, m, tools=None, **kw):
                return EngineChatResponse(
                    text="ok",
                    usage=EngineUsage(prompt_tokens=500, completion_tokens=100, total_tokens=600),
                )

            async def aclose(self):
                return None

        runtime_module.build_provider = lambda *a, **k: P()
        record = self.service.create_task({"prompt": "usage check"})
        deadline = time.time() + 20
        events = []
        while time.time() < deadline:
            events = self.service.task_events(record["id"])
            if any(e["type"] == "task-done" for e in events):
                break
            time.sleep(0.03)

        usage_events = [
            e for e in events if e["type"] in ("token-usage", "context-usage")
        ]
        self.assertTrue(usage_events, "no usage events were emitted")
        for event in usage_events:
            self.assertEqual(
                event["payload"].get("context_window"),
                1_000_000,
                f"{event['type']} still reports the default window",
            )
