"""Memory the agent can write, and memory the user can edit.

The regression behind this file: the agent was asked "can you write memory?" and
correctly answered no, because no such capability existed and the system prompt
never mentioned one. Both halves - the tool and the prompt text that advertises it
- are needed, so both are asserted here.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from onion_harness.api.secrets import MemorySecretStore, cipher_from_store
from onion_harness.core import EventType, Task
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineToolCall, EngineUsage
from onion_harness.harness.memory_tools import MAX_WRITES_PER_TASK, MemoryTools
from onion_harness.ledger import ChangeLedger, LedgerStore
from onion_harness.memory import (
    MemoryBridge,
    MemoryKind,
    MemoryStore,
    MemoryStoreError,
    new_memory,
)
from onion_harness.plugins import builtin_executors
from onion_harness.sandbox import Sandbox


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-memrw-"))
        # One secret store per test: reopening with a fresh one would derive a
        # different key and make "decryption failed" indistinguishable from a
        # genuine edit bug.
        self.secrets = MemorySecretStore()
        self.store = MemoryStore(self.dir / "m.db", cipher=cipher_from_store(self.secrets))
        self.bridge = MemoryBridge(self.store)

    def tearDown(self):
        self.store.close()


class StoreEditTest(_StoreBase):
    def test_get_returns_a_stored_memory(self):
        memory = new_memory("原始文本", MemoryKind.FACT)
        self.store.remember(memory)
        fetched = self.store.get(memory.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.text, "原始文本")

    def test_get_of_a_missing_id_is_none(self):
        self.assertIsNone(self.store.get("mem_nope"))

    def test_update_changes_text_kind_and_weight(self):
        memory = new_memory("old text", MemoryKind.FACT, weight=0.3)
        self.store.remember(memory)

        updated = self.store.update(
            memory.id, text="new text", kind="preference", weight=0.9
        )
        self.assertEqual(updated.text, "new text")
        self.assertEqual(updated.kind, MemoryKind.PREFERENCE)
        self.assertAlmostEqual(updated.weight, 0.9)

        reread = self.store.get(memory.id)
        self.assertEqual(reread.text, "new text")

    def test_update_reindexes_so_search_follows_the_edit(self):
        """The old wording must stop matching and the new wording must start.

        Editing without rebuilding the index would leave the previous text
        findable - the most confusing possible outcome of an edit.
        """
        memory = new_memory("alpha beta gamma")
        self.store.remember(memory)
        self.assertEqual(len(self.store.recall("alpha")), 1)

        self.store.update(memory.id, text="delta epsilon zeta")
        self.assertEqual(self.store.recall("alpha"), [], "stale index entry survived")
        self.assertEqual(len(self.store.recall("delta")), 1)

    def test_edit_keeps_the_file_encrypted(self):
        memory = new_memory("initial marker")
        self.store.remember(memory)
        self.store.update(memory.id, text="EDITEDSECRET")
        self.store.close()

        raw = (self.dir / "m.db").read_bytes()
        self.assertNotIn(b"EDITEDSECRET", raw)
        # Reopen and confirm it reads back.
        reopened = MemoryStore(
            self.dir / "m.db", cipher=cipher_from_store(self.secrets)
        )
        try:
            self.assertEqual(reopened.get(memory.id).text, "EDITEDSECRET")
        finally:
            reopened.close()

    def test_empty_text_is_rejected(self):
        memory = new_memory("something")
        self.store.remember(memory)
        with self.assertRaises(MemoryStoreError):
            self.store.update(memory.id, text="   ")

    def test_unknown_kind_is_rejected(self):
        memory = new_memory("something")
        self.store.remember(memory)
        with self.assertRaises(MemoryStoreError):
            self.store.update(memory.id, kind="nonsense")

    def test_update_of_a_missing_id_returns_none(self):
        self.assertIsNone(self.store.update("mem_nope", text="x"))


class BridgeEditTest(_StoreBase):
    def test_add_creates_a_note(self):
        record = self.bridge.add("用户偏好使用 PowerShell", kind="preference")
        self.assertEqual(record["kind"], "preference")
        self.assertEqual(self.store.count(), 1)
        self.assertTrue(self.bridge.recall("PowerShell"))

    def test_add_rejects_blank_text(self):
        with self.assertRaises(MemoryStoreError):
            self.bridge.add("   ")

    def test_update_returns_the_record(self):
        record = self.bridge.add("first")
        updated = self.bridge.update(record["id"], text="second")
        self.assertEqual(updated["text"], "second")

    def test_update_of_a_missing_id_returns_none(self):
        self.assertIsNone(self.bridge.update("mem_nope", text="x"))


class MemoryToolsTest(_StoreBase):
    def _tools(self, bridge=None):
        return MemoryTools(bridge or self.bridge, "t1")

    def _by_name(self, tools):
        return {tool.name: tool for tool in tools}

    def test_tools_are_exposed(self):
        tools = self._by_name(self._tools().tools())
        self.assertEqual(set(tools), {"memory_remember", "memory_search", "memory_forget"})

    def test_remember_writes_a_note(self):
        tool = self._by_name(self._tools().tools())["memory_remember"]
        result = tool.handler({"text": "用户偏好使用 PowerShell", "kind": "preference"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.recent()[0].kind, MemoryKind.PREFERENCE)

    def test_remember_defaults_to_fact(self):
        tool = self._by_name(self._tools().tools())["memory_remember"]
        tool.handler({"text": "项目在 D:/work"})
        self.assertEqual(self.store.recent()[0].kind, MemoryKind.FACT)

    def test_remember_rejects_blank(self):
        tool = self._by_name(self._tools().tools())["memory_remember"]
        result = tool.handler({"text": "   "})
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.count(), 0)

    def test_write_budget_is_enforced(self):
        tool = self._by_name(self._tools().tools())["memory_remember"]
        for index in range(MAX_WRITES_PER_TASK):
            self.assertTrue(tool.handler({"text": f"note {index}"})["ok"])
        blocked = tool.handler({"text": "one too many"})
        self.assertFalse(blocked["ok"])
        self.assertIn("limit", blocked["error"].lower())
        self.assertEqual(self.store.count(), MAX_WRITES_PER_TASK)

    def test_search_finds_what_was_written(self):
        tools = self._by_name(self._tools().tools())
        tools["memory_remember"].handler({"text": "用户偏好使用 PowerShell 写脚本"})
        result = tools["memory_search"].handler({"query": "PowerShell"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertIn("PowerShell", result["memories"][0]["text"])

    def test_search_without_a_query_lists_recent_notes(self):
        """An empty query is a "what do you have" request, not an error."""
        tools = self._by_name(self._tools().tools())
        result = tools["memory_search"].handler({"query": "  "})
        self.assertTrue(result["ok"])
        self.assertFalse(result["matched"])

    def test_forget_removes_a_note(self):
        tools = self._by_name(self._tools().tools())
        record = self.bridge.add("temporary note")
        result = tools["memory_forget"].handler({"id": record["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.count(), 0)

    def test_forget_of_unknown_id_reports_failure(self):
        tools = self._by_name(self._tools().tools())
        self.assertFalse(tools["memory_forget"].handler({"id": "mem_nope"})["ok"])

    def test_no_bridge_means_no_tools_are_registered(self):
        """The runtime must not offer memory tools when memory is disabled."""
        from onion_harness.harness.memory_tools import build_memory_tools

        self.assertEqual(build_memory_tools(None, "t1"), [])

    def test_handlers_refuse_when_memory_is_disabled(self):
        """Even if one were reached, it fails loudly instead of pretending."""
        tools = self._by_name(MemoryTools(None, "t1").tools())
        for name in tools:
            result = tools[name].handler({"text": "x", "query": "x", "id": "x"})
            self.assertFalse(result["ok"], name)
            self.assertIn("disabled", result["error"], name)

    def test_write_emits_an_event(self):
        seen = []
        tools = self._by_name(MemoryTools(self.bridge, "t1", emit=lambda t, r: seen.append(r)).tools())
        tools["memory_remember"].handler({"text": "remembered thing"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["text"], "remembered thing")


class _FakeProvider:
    def __init__(self, script, systems):
        self.model = "fake"
        self.last_reasoning = ""
        self._script = script
        self._i = 0
        self._systems = systems

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        for message in messages:
            if getattr(message, "type", "") == "system":
                self._systems.append(getattr(message, "text", ""))
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(5, 2, 7),
            model=self.model,
        )


class RuntimeMemoryToolsTest(_StoreBase):
    def setUp(self):
        super().setUp()
        self.systems = []
        self.out_of_band = []
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self._original = runtime_module.build_provider

    def tearDown(self):
        runtime_module.build_provider = self._original
        self.ledger.close()
        super().tearDown()

    def _run(self, script_text, *, memory=True, mode="strict"):
        from onion_harness.harness.runtime import OnionRuntime, build_model_config

        runtime_module.build_provider = lambda *a, **k: _FakeProvider(script_text, self.systems)
        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
            memory=self.bridge if memory else None,
        )
        runtime.set_approval_listener(
            lambda _task_id, payload: self.out_of_band.append(payload)
        )
        model = build_model_config(
            {"provider_id": "deepseek", "api_key": "sk-test", "mode": mode}
        )
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt="remember this", workspace=str(self.dir)), model):
                events.append(ev)

        asyncio.run(consume())
        return events

    def test_system_prompt_advertises_the_memory_tools(self):
        """This is what the agent reads when asked "can you write memory?"."""
        self._run([{"text": "ok"}])
        prompt = self.systems[0]
        self.assertIn("Long-term memory", prompt)
        self.assertIn("memory_remember", prompt)
        self.assertIn("you can write to it", prompt)

    def test_no_memory_means_no_claim_of_memory(self):
        self._run([{"text": "ok"}], memory=False)
        self.assertNotIn("memory_remember", self.systems[0])

    def test_agent_can_write_a_memory_through_a_tool_call(self):
        events = self._run(
            [
                {
                    "tool_calls": [
                        EngineToolCall(
                            id="c1",
                            name="memory_remember",
                            arguments={"text": "用户偏好使用 PowerShell", "kind": "preference"},
                        )
                    ]
                },
                {"text": "saved"},
            ]
        )
        self.assertEqual(self.store.count(), 1)
        self.assertIn("PowerShell", self.store.recent()[0].text)

        # The write is reported to the GUI as an agent-authored learning.
        learned = [item for item in self.out_of_band if item["type"] == "memory-learned"]
        self.assertEqual(len(learned), 1)
        self.assertEqual(learned[0]["payload"]["by"], "agent")
        self.assertIn("PowerShell", learned[0]["payload"]["memory"]["text"])

    def test_agent_written_note_is_recalled_on_the_next_run(self):
        self._run(
            [
                {
                    "tool_calls": [
                        EngineToolCall(
                            id="c1",
                            name="memory_remember",
                            arguments={"text": "用户偏好使用 PowerShell 写脚本", "kind": "preference"},
                        )
                    ]
                },
                {"text": "saved"},
            ]
        )
        self.systems.clear()
        self._run([{"text": "ok"}])
        # The prompt carries the note the agent itself stored, through the normal
        # recall path rather than any special case.
        self.assertIn("PowerShell", self.systems[0])


if __name__ == "__main__":
    unittest.main()

class UnreadableRowsTest(unittest.TestCase):
    """Rows from a different master secret must not break the whole store."""

    def setUp(self):
        from onion_harness.api.secrets import DataCipher

        self.dir = Path(tempfile.mkdtemp(prefix="onion-badkey-"))
        self.path = self.dir / "m.db"
        self.key = b"a" * 32
        writer = MemoryStore(self.path, cipher=DataCipher(self.key))
        writer.remember(new_memory("older note", MemoryKind.FACT))
        writer.close()

    def test_remember_works_despite_an_unreadable_row(self):
        """The reported symptom: adding appeared to work while reads did not.

        The real failure was in the write path - _find_duplicate decrypted every
        row and raised, so nothing could be added to a database that merely
        contained older notes from a different key.
        """
        from onion_harness.api.secrets import DataCipher

        store = MemoryStore(self.path, cipher=DataCipher(b"b" * 32))
        try:
            store.remember(new_memory("a new note", MemoryKind.PREFERENCE))
            self.assertEqual(store.count(), 2)
        finally:
            store.close()

    def test_recall_skips_unreadable_rows(self):
        from onion_harness.api.secrets import DataCipher

        store = MemoryStore(self.path, cipher=DataCipher(b"b" * 32))
        try:
            store.remember(new_memory("findable new note about PowerShell", MemoryKind.FACT))
            hits = store.recall("PowerShell")
            self.assertEqual(len(hits), 1)
            self.assertIn("findable", hits[0].text)
        finally:
            store.close()

    def test_recent_and_unreadable_count_agree(self):
        from onion_harness.api.secrets import DataCipher

        store = MemoryStore(self.path, cipher=DataCipher(b"b" * 32))
        try:
            self.assertEqual(store.recent(10), [])
            self.assertEqual(store.unreadable_count(), 1)
        finally:
            store.close()


class SearchListingFallbackTest(unittest.TestCase):
    """A question phrased without the note's words must still find it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-searchfb-"))
        from onion_harness.api.secrets import DataCipher

        self.store = MemoryStore(self.dir / "m.db", cipher=DataCipher(b"k" * 32))
        self.bridge = MemoryBridge(self.store)
        self.bridge.add("用户偏好：脚本统一用 PowerShell 7", kind="preference")

    def tearDown(self):
        self.store.close()

    def _search(self, query):
        return self.tools["memory_search"].handler({"query": query})

    @property
    def tools(self):
        return {tool.name: tool for tool in MemoryTools(self.bridge, "t1").tools()}

    def test_a_matching_query_reports_a_match(self):
        result = self._search("PowerShell")
        self.assertTrue(result["matched"])
        self.assertGreaterEqual(result["count"], 1)

    def test_a_non_matching_query_lists_recent_notes(self):
        result = self._search("你还记得什么")
        self.assertFalse(result["matched"])
        self.assertGreaterEqual(result["count"], 1, "the agent must still see its notes")
        self.assertIn("most recent", result["note"])

    def test_no_query_lists_notes(self):
        result = self._search("")
        self.assertFalse(result["matched"])
        self.assertGreaterEqual(result["count"], 1)

    def test_disabled_memory_refuses_every_call(self):
        """The tools exist but fail loudly; the runtime decides whether to register
        them at all (see build_memory_tools)."""
        tools = {tool.name: tool for tool in MemoryTools(None, "t1").tools()}
        self.assertTrue(tools, "handlers must still be constructible")
        for name, tool in tools.items():
            result = tool.handler({"text": "x", "query": "x", "id": "x"})
            self.assertFalse(result["ok"], name)
            self.assertIn("disabled", result["error"], name)

    def test_the_prompt_explains_recall_is_keyword_based(self):
        from onion_harness.harness.runtime import MEMORY_PROMPT_HINT

        self.assertIn("keyword", MEMORY_PROMPT_HINT)
        self.assertIn("memory_search", MEMORY_PROMPT_HINT)
