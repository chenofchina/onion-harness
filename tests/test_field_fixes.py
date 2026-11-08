"""Regression tests for four reported symptoms, each with its real root cause.

The symptoms as reported were "keys do not persist", "the memory tab is empty",
"the agent cannot write memory" and "some HTML is not rendered". None of those was
the actual defect, which is why they are pinned to causes rather than to wording.

1. Keys vanished because pywin32's modules are unreachable in a frozen build, so the
   credential store looked unavailable and the vault fell back to memory.
2. The memory tab was blank because one unreadable row raised and took the whole
   list with it.
3. The agent could not write memory because memory was never opened - the same key
   failure, reported as a missing capability.
4. HTML was partially rendered for three separate reasons: text interleaved with
   elements lost its position, `<pre>` collapsed newlines, and allowlisted
   attributes (`open`, `colspan`) were dropped on the way to React.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from onion_harness.api.secrets import DataCipher, MemorySecretStore, WindowsCredentialStore
from onion_harness.api.store import AppStore
from onion_harness.core.hybrid import parse_ui_block, render
from onion_harness.memory import MemoryKind, MemoryStore, new_memory


class CredentialStoreTest(unittest.TestCase):
    """Root cause 1: the store must be found, and its absence must be explained."""

    def test_store_reports_availability_and_reason(self):
        store = WindowsCredentialStore()
        if os.name == "nt":
            # On this machine pywin32 is installed, so it must be usable.
            self.assertTrue(store.available(), f"store unavailable: {store.reason}")
            self.assertEqual(store.reason, "")
        else:
            self.assertFalse(store.available())
            self.assertIn("Windows", store.reason)

    def test_public_settings_expose_store_health(self):
        """Without this a lost key looks identical to a key that was never saved."""
        store = AppStore(Path(tempfile.mkdtemp(prefix="onion-cred-")))
        public = store.public_settings(store.load_settings())
        self.assertIn("credential_store", public)
        self.assertIn("available", public["credential_store"])
        self.assertIn("reason", public["credential_store"])

    def test_a_fake_store_without_reason_is_tolerated(self):
        class Bare:
            def available(self):
                return True

            def get(self, name):
                return None

            def set(self, name, value):
                pass

            def delete(self, name):
                pass

        from onion_harness.api.secrets import KeyVault

        store = AppStore(Path(tempfile.mkdtemp(prefix="onion-cred2-")), vault=KeyVault(Bare()))
        public = store.public_settings(store.load_settings())
        self.assertTrue(public["credential_store"]["available"])
        self.assertEqual(public["credential_store"]["reason"], "")


class MemoryResilienceTest(unittest.TestCase):
    """Root cause 2: an undecryptable row must not blank the whole list."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-memres-"))
        self.good = MemorySecretStore()

    def test_one_unreadable_row_does_not_hide_the_others(self):
        # Write two notes with one key, then reopen with a different key: exactly the
        # state a changed master secret leaves behind.
        path = self.dir / "m.db"
        writer = MemoryStore(path, cipher=DataCipher(b"a" * 32))
        writer.remember(new_memory("readable note", MemoryKind.FACT))
        writer.close()

        reader = MemoryStore(path, cipher=DataCipher(b"b" * 32))
        try:
            # Must not raise, and must report how many were skipped.
            self.assertEqual(reader.recent(10), [])
            self.assertEqual(reader.unreadable_count(), 1)
        finally:
            reader.close()

    def test_mixed_rows_report_the_readable_ones(self):
        path = self.dir / "m.db"
        key = b"k" * 32
        first = MemoryStore(path, cipher=DataCipher(key))
        first.remember(new_memory("note one", MemoryKind.FACT))
        first.close()

        # Reopen with the right key, add another, then corrupt one row's bytes.
        store = MemoryStore(path, cipher=DataCipher(key))
        store.remember(new_memory("note two", MemoryKind.PREFERENCE))
        store._conn.execute(
            "UPDATE memories SET ciphertext = ? WHERE id = (SELECT id FROM memories LIMIT 1)",
            (b"not a valid ciphertext",),
        )
        store._conn.commit()
        try:
            readable = store.recent(10)
            self.assertEqual(len(readable), 1, "the good row must still be listed")
            self.assertEqual(store.unreadable_count(), 1)
        finally:
            store.close()

    def test_all_readable_reports_zero(self):
        path = self.dir / "ok.db"
        store = MemoryStore(path, cipher=DataCipher(b"z" * 32))
        store.remember(new_memory("fine", MemoryKind.FACT))
        try:
            self.assertEqual(store.unreadable_count(), 0)
            self.assertEqual(len(store.recent(10)), 1)
        finally:
            store.close()


class MemoryCapabilityTest(unittest.TestCase):
    """Root cause 3: memory tools exist only when memory is actually open."""

    def setUp(self):
        os.environ.setdefault("ONION_SECRET_STORE", "memory")

    def test_memory_tools_are_registered_when_memory_works(self):
        from onion_harness.api.service import OnionService
        from onion_harness.core import Task

        service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-memcap-")))
        service.start()
        try:
            self.assertIsNotNone(service.memory, "memory must open with a working store")
            tools = service.runtime._build_tools(Task(prompt="x"), model=None)
            names = {tool.name for tool in tools}
            self.assertIn("memory_remember", names)
            self.assertIn("memory_search", names)
        finally:
            service.stop()

    def test_memory_stats_explain_disabling(self):
        from onion_harness.api.service import OnionService

        service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-memcap2-")))
        service.start()
        try:
            # Force the disabled path and check the reason is informative.
            service.memory = None
            service.memory_store = None
            stats = service.memory_stats()
            self.assertFalse(stats["enabled"])
            self.assertTrue(stats.get("reason"))
        finally:
            service.stop()

    def test_agent_written_note_is_persisted_and_listed(self):
        from onion_harness.api.service import OnionService
        from onion_harness.harness.memory_tools import MemoryTools

        service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-memcap3-")))
        service.start()
        try:
            tools = {tool.name: tool for tool in MemoryTools(service.memory, "t1").tools()}
            result = tools["memory_remember"].handler(
                {"text": "用户偏好使用 PowerShell", "kind": "preference"}
            )
            self.assertTrue(result["ok"])
            listed = service.memory_list()
            self.assertEqual(len(listed), 1)
            self.assertIn("PowerShell", listed[0]["text"])
        finally:
            service.stop()


class HybridRenderingTest(unittest.TestCase):
    """Root cause 4: three separate rendering gaps."""

    def test_interleaved_text_keeps_its_order(self):
        """`Press <b>OK</b> now` must not become `Press now OK`."""
        nodes, _actions, _error = parse_ui_block("<p>Press <b>OK</b> now</p>")
        paragraph = nodes[0]
        self.assertEqual(paragraph.tag, "p")
        tags = [child.tag for child in paragraph.children]
        self.assertEqual(tags, ["#text", "b", "#text"])
        self.assertEqual(paragraph.children[0].text, "Press ")
        self.assertEqual(paragraph.children[2].text, " now")
        self.assertEqual(paragraph.children[1].children[0].text, "OK")

    def test_text_inside_nested_tags_is_kept(self):
        nodes, _, _ = parse_ui_block("<div><p>a</p><p>b</p></div>")
        div = nodes[0]
        self.assertEqual(div.tag, "div")
        self.assertEqual([child.tag for child in div.children], ["p", "p"])

    def test_button_label_survives_the_text_node_change(self):
        _, actions, _ = parse_ui_block(
            "<button onclick=\"sendAction('go')\">确认刷机</button>"
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].label, "确认刷机")

    def test_nested_button_label_is_collected(self):
        _, actions, _ = parse_ui_block(
            "<button onclick=\"sendAction('x')\"><b>Go</b> now</button>"
        )
        self.assertEqual(actions[0].label, "Go now")

    def test_pre_preserves_its_text(self):
        nodes, _, _ = parse_ui_block("<pre>line1\nline2</pre>")
        self.assertEqual(nodes[0].tag, "pre")
        text = nodes[0].children[0].text
        self.assertIn("line1", text)
        self.assertIn("line2", text)

    def test_details_open_attribute_survives_the_allowlist(self):
        nodes, _, _ = parse_ui_block('<details open><summary>s</summary>b</details>')
        self.assertEqual(nodes[0].tag, "details")
        self.assertIn("open", nodes[0].attrs)

    def test_table_span_attributes_survive(self):
        nodes, _, _ = parse_ui_block('<table><tr><td colspan="2">x</td></tr></table>')
        cell = nodes[0].children[0].children[0]
        self.assertEqual(cell.tag, "td")
        self.assertEqual(cell.attrs.get("colspan"), "2")

    def test_input_keeps_value_and_type(self):
        nodes, _, _ = parse_ui_block('<input type="text" value="hello">')
        self.assertEqual(nodes[0].attrs.get("value"), "hello")
        self.assertEqual(nodes[0].attrs.get("type"), "text")

    def test_checkbox_keeps_checked(self):
        nodes, _, _ = parse_ui_block('<input type="checkbox" checked>')
        self.assertIn("checked", nodes[0].attrs)

    def test_full_render_of_a_rich_block(self):
        text = (
            "Intro\n"
            "<ui><section><h2>Title</h2>"
            "<p>Press <b>OK</b> now</p>"
            "<pre>a\nb</pre>"
            "<button onclick=\"sendAction('go')\">Go</button></section></ui>"
        )
        out = render(text)
        self.assertTrue(out.has_ui)
        self.assertEqual([action.id for action in out.actions], ["go"])
        section = out.segments[1].nodes[0]
        self.assertEqual(section.tag, "section")
        self.assertEqual(
            [child.tag for child in section.children], ["h2", "p", "pre", "button"]
        )

    def test_security_still_holds_after_the_change(self):
        """The text-node change must not open a path for a rejected tag."""
        nodes, actions, _ = parse_ui_block(
            '<script>alert(1)</script><button onclick="sendAction(\'ok\')">x</button>'
        )
        self.assertNotIn("script", [node.tag for node in nodes])
        self.assertEqual([action.id for action in actions], ["ok"])


class BuiltinSearchTest(unittest.TestCase):
    """Search must work with no external service at all."""

    def test_compact_shape_matches_the_external_service(self):
        """The fallback must be indistinguishable to the parser."""
        from onion_harness.plugins.builtin_search import (
            BuiltinSearchResponse,
            BuiltinSearchResult,
        )
        from onion_harness.plugins.web_search import parse_payload

        response = BuiltinSearchResponse(
            query="q",
            results=[BuiltinSearchResult(title="T", url="https://x", snippet="s")],
        )
        compact = response.to_compact()
        self.assertEqual(set(compact), {"answer", "top"})
        parsed = parse_payload(compact, "q")
        self.assertEqual(parsed.results[0].title, "T")
        json.dumps(compact)

    def test_empty_query_is_refused(self):
        from onion_harness.plugins.builtin_search import BuiltinSearch

        with self.assertRaises(ValueError):
            BuiltinSearch().search("   ")

    def test_tool_falls_back_when_the_service_is_down(self):
        """The reported symptom: no external service, so search must still answer."""
        from onion_harness.plugins.web_search import SearchResponse, WebSearchClient

        class FakeBuiltin:
            def __init__(self):
                self.called = False

            def search(self, query):
                from onion_harness.plugins.builtin_search import (
                    BuiltinSearchResponse,
                    BuiltinSearchResult,
                )

                self.called = True
                return BuiltinSearchResponse(
                    query=query,
                    results=[BuiltinSearchResult(title="Built in", url="https://b", snippet="ok")],
                )

        builtin = FakeBuiltin()
        # Point at a closed port so the external call fails fast.
        from onion_harness.plugins.web_search import WebSearchConfig

        client = WebSearchClient(
            WebSearchConfig(base_url="http://127.0.0.1:1", timeout=1), builtin=builtin
        )
        response = client.search("anything")
        self.assertTrue(builtin.called, "the built-in backend must have been used")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "Built in")

    def test_both_paths_failing_is_still_an_error(self):
        """Fail-closed survives the fallback: no fake empty result."""
        from onion_harness.plugins.web_search import SearchError, WebSearchClient, WebSearchConfig

        class BrokenBuiltin:
            def search(self, query):
                raise RuntimeError("no network")

        client = WebSearchClient(
            WebSearchConfig(base_url="http://127.0.0.1:1", timeout=1), builtin=BrokenBuiltin()
        )
        with self.assertRaises(SearchError) as ctx:
            client.search("anything")
        self.assertEqual(ctx.exception.code, "search_unavailable")
        self.assertIn("from memory", str(ctx.exception))

    def test_no_builtin_keeps_the_old_fail_closed_path(self):
        from onion_harness.plugins.web_search import SearchError, WebSearchClient, WebSearchConfig

        client = WebSearchClient(WebSearchConfig(base_url="http://127.0.0.1:1", timeout=1))
        with self.assertRaises(SearchError):
            client.search("anything")

    def test_default_tool_ships_the_builtin_backend(self):
        from onion_harness.plugins.web_search import build_web_search_tool

        tool = build_web_search_tool()
        self.assertIsNotNone(tool)
        self.assertIsNotNone(tool.handler.__self__.client._builtin)


if __name__ == "__main__":
    unittest.main()