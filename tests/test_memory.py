"""Layer 2 tests - memory model, tokenizer, store and extraction."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from onion_harness.memory import MemoryExtractor, MemoryStore, new_memory
from onion_harness.memory.model import Memory, MemoryKind
from onion_harness.memory.tokenizer import (
    build_match_expression,
    index_text,
    query_terms,
    tokenize,
)


class TokenizerTest(unittest.TestCase):
    def test_latin_words_are_lowercased(self):
        self.assertEqual(tokenize("PowerShell"), ["powershell"])

    def test_chinese_produces_bigrams(self):
        tokens = tokenize("偏好")
        self.assertIn("偏好", tokens)

    def test_long_chinese_run_yields_overlapping_bigrams(self):
        tokens = tokenize("用户偏好")
        self.assertEqual(tokens, ["用户", "户偏", "偏好"])

    def test_single_cjk_character_is_kept(self):
        self.assertEqual(tokenize("好"), ["好"])

    def test_mixed_text_handles_both(self):
        self.assertEqual(tokenize("用 PowerShell"), ["powershell", "用"])

    def test_empty_input(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(index_text(""), "")
        self.assertEqual(build_match_expression(""), "")

    def test_match_expression_ors_quoted_terms(self):
        expression = build_match_expression("偏好")
        self.assertEqual(expression, '"偏好"')

    def test_query_terms_are_deduplicated(self):
        terms = query_terms("pytest pytest pytest")
        self.assertEqual(terms, ["pytest"])

    def test_quotes_cannot_break_the_expression(self):
        # A quote in the query must not become an FTS operator.
        expression = build_match_expression('py"test')
        self.assertNotIn('py"test', expression)


class MemoryModelTest(unittest.TestCase):
    def test_text_is_normalised(self):
        memory = Memory(text="  hello   world \n")
        self.assertEqual(memory.text, "hello world")

    def test_weight_is_clamped(self):
        self.assertEqual(Memory(text="x", weight=5.0).weight, 1.0)
        self.assertEqual(Memory(text="x", weight=-1.0).weight, 0.0)

    def test_preferences_do_not_decay(self):
        memory = Memory(text="x", kind=MemoryKind.PREFERENCE, weight=0.9)
        old = time.time() - 365 * 86_400
        memory.last_used_at = old
        self.assertAlmostEqual(memory.effective_weight(), 0.9, places=2)

    def test_events_decay_but_keep_a_floor(self):
        memory = Memory(text="x", kind=MemoryKind.EVENT, weight=0.8)
        memory.last_used_at = time.time() - 30 * 86_400
        self.assertLess(memory.effective_weight(), 0.8)
        self.assertGreaterEqual(memory.effective_weight(), 0.24)

    def test_touch_updates_recency_and_count(self):
        memory = Memory(text="x")
        memory.last_used_at = 0.0
        memory.touch()
        self.assertGreater(memory.last_used_at, 0.0)
        self.assertEqual(memory.use_count, 1)

    def test_unknown_kind_falls_back_to_fact(self):
        self.assertEqual(new_memory("x", "nonsense").kind, MemoryKind.FACT)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-mem-"))
        self.store = MemoryStore(self.dir / "memory.sqlite3")

    def tearDown(self):
        self.store.close()

    def test_remember_and_count(self):
        self.store.remember(new_memory("hello world memory"))
        self.assertEqual(self.store.count(), 1)

    def test_duplicate_text_is_merged_not_added(self):
        first = self.store.remember(new_memory("same text here"))
        second = self.store.remember(new_memory("same text here"))
        self.assertEqual(first, second)
        self.assertEqual(self.store.count(), 1)

    def test_duplicate_of_a_different_kind_is_kept_separate(self):
        self.store.remember(new_memory("shared text", MemoryKind.FACT))
        self.store.remember(new_memory("shared text", MemoryKind.EVENT))
        self.assertEqual(self.store.count(), 2)

    def test_forget_removes_the_fts_row_too(self):
        memory_id = self.store.remember(new_memory("will be removed entirely"))
        self.assertTrue(self.store.forget(memory_id))
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.recall("removed"), [])

    def test_recall_finds_chinese_substring(self):
        self.store.remember(new_memory("用户偏好使用 PowerShell", MemoryKind.PREFERENCE))
        hits = self.store.recall("偏好")
        self.assertEqual(len(hits), 1)
        self.assertIn("PowerShell", hits[0].text)

    def test_recall_matches_latin_case_insensitively(self):
        self.store.remember(new_memory("Always run PYTEST before deploy"))
        self.assertEqual(len(self.store.recall("pytest")), 1)

    def test_recall_returns_nothing_for_unrelated_query(self):
        self.store.remember(new_memory("项目根目录在 D:/work"))
        self.assertEqual(self.store.recall("quantum chromodynamics"), [])

    def test_recall_respects_the_limit(self):
        for index in range(8):
            self.store.remember(new_memory(f"deployment note number {index}"))
        self.assertLessEqual(len(self.store.recall("deployment", limit=3)), 3)

    def test_recall_filters_by_kind(self):
        self.store.remember(new_memory("deployment fact", MemoryKind.FACT))
        self.store.remember(new_memory("deployment event", MemoryKind.EVENT))
        facts = self.store.recall("deployment", kinds=["fact"])
        self.assertEqual([m.kind for m in facts], [MemoryKind.FACT])

    def test_recent_is_ordered_by_use(self):
        a = self.store.remember(new_memory("older memory alpha"))
        self.store.remember(new_memory("newer memory beta"))
        self.store.touch(a)
        recent = self.store.recent(limit=5)
        self.assertEqual(recent[0].id, a)

    def test_purge_clears_everything(self):
        self.store.remember(new_memory("one"))
        self.store.remember(new_memory("two"))
        self.assertEqual(self.store.purge(), 2)
        self.assertEqual(self.store.count(), 0)

    def test_stats_report_kinds(self):
        self.store.remember(new_memory("a fact", MemoryKind.FACT))
        self.store.remember(new_memory("a habit", MemoryKind.CODE_HABIT))
        stats = self.store.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_kind"]["fact"], 1)

    def test_persistence_across_reopen(self):
        self.store.remember(new_memory("survives a restart", MemoryKind.PREFERENCE))
        self.store.close()
        reopened = MemoryStore(self.dir / "memory.sqlite3")
        try:
            self.assertEqual(reopened.count(), 1)
            self.assertEqual(len(reopened.recall("restart")), 1)
        finally:
            reopened.close()
        self.store = MemoryStore(self.dir / "other.sqlite3")

    def test_text_is_not_written_in_plaintext_when_encrypted(self):
        """The database file must not contain the memory's text."""
        class Rot13Cipher:
            def encrypt(self, text: str) -> bytes:
                return text.encode("utf-8")[::-1]

            def decrypt(self, raw: bytes) -> str:
                return bytes(raw)[::-1].decode("utf-8")

        path = self.dir / "enc.sqlite3"
        store = MemoryStore(path, cipher=Rot13Cipher())
        try:
            store.remember(new_memory("SECRETMARKER not in the file"))
        finally:
            store.close()

        raw = path.read_bytes()
        self.assertNotIn(b"SECRETMARKER", raw)

        reopened = MemoryStore(path, cipher=Rot13Cipher())
        try:
            hits = reopened.recall("SECRETMARKER")
            self.assertEqual(len(hits), 1)
            self.assertIn("SECRETMARKER", hits[0].text)
        finally:
            reopened.close()


class ExtractorTest(unittest.TestCase):
    def test_extracts_a_stated_preference(self):
        events = [{"type": "user-input", "payload": {"text": "以后都用 PowerShell 写脚本"}}]
        memories = MemoryExtractor().extract(events, task_id="t1")
        kinds = {m.kind for m in memories}
        self.assertIn(MemoryKind.PREFERENCE, kinds)
        self.assertTrue(all(m.source_task == "t1" for m in memories))

    def test_extracts_a_path_fact(self):
        events = [{"type": "user-input", "payload": {"text": "项目在 D:/work/onion 下面"}}]
        memories = MemoryExtractor().extract(events)
        self.assertTrue(any("D:/work/onion" in m.text for m in memories))

    def test_extracts_a_failure(self):
        events = [
            {
                "type": "tool-result",
                "payload": {"is_error": True, "content": "Error: Access denied to C:/Windows"},
            }
        ]
        memories = MemoryExtractor().extract(events)
        self.assertTrue(any(m.kind is MemoryKind.EVENT for m in memories))

    def test_repeated_executor_becomes_a_habit(self):
        events = [{"type": "tool-call", "payload": {"executor": "python"}}] * 3
        memories = MemoryExtractor().extract(events)
        habits = [m for m in memories if m.kind is MemoryKind.CODE_HABIT]
        self.assertEqual(len(habits), 1)
        self.assertIn("python", habits[0].text)

    def test_single_executor_use_is_not_a_habit(self):
        events = [{"type": "tool-call", "payload": {"executor": "cmd"}}]
        memories = MemoryExtractor().extract(events)
        self.assertEqual([m for m in memories if m.kind is MemoryKind.CODE_HABIT], [])

    def test_short_and_empty_text_is_dropped(self):
        events = [{"type": "user-input", "payload": {"text": "hi"}}]
        self.assertEqual(MemoryExtractor().extract(events), [])

    def test_duplicates_within_a_task_collapse(self):
        events = [
            {"type": "user-input", "payload": {"text": "以后都用 PowerShell"}},
            {"type": "user-input", "payload": {"text": "以后都用 PowerShell"}},
        ]
        memories = MemoryExtractor().extract(events)
        self.assertEqual(len([m for m in memories if m.kind is MemoryKind.PREFERENCE]), 1)

    def test_events_without_payload_are_safe(self):
        self.assertEqual(MemoryExtractor().extract([{"type": "task-done"}]), [])

    def test_summarizer_can_enrich_results(self):
        def enrich(memories):
            return memories + [new_memory("added by summarizer", MemoryKind.FACT)]

        events = [{"type": "user-input", "payload": {"text": "以后都用 PowerShell"}}]
        memories = MemoryExtractor(summarizer=enrich).extract(events)
        self.assertTrue(any(m.text == "added by summarizer" for m in memories))

    def test_broken_summarizer_does_not_lose_rule_output(self):
        def broken(_memories):
            raise RuntimeError("model unavailable")

        events = [{"type": "user-input", "payload": {"text": "以后都用 PowerShell"}}]
        memories = MemoryExtractor(summarizer=broken).extract(events)
        self.assertTrue(memories, "rule output survived the summarizer failure")


class ExtractionToStoreTest(unittest.TestCase):
    """The two halves must fit together: extract, store, recall."""

    def test_a_task_then_recalls_its_preference(self):
        store = MemoryStore(Path(tempfile.mkdtemp(prefix="onion-mem2-")) / "m.sqlite3")
        try:
            events = [
                {"type": "user-input", "payload": {"text": "以后都用 PowerShell 执行脚本"}},
                {"type": "tool-call", "payload": {"executor": "powershell"}},
                {"type": "tool-call", "payload": {"executor": "powershell"}},
            ]
            store.remember_many(MemoryExtractor().extract(events, task_id="t9"))
            hits = store.recall("PowerShell")
            self.assertTrue(hits)
            self.assertTrue(all(m.source_task == "t9" for m in hits))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()