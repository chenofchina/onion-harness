"""Web search integration: parsing, failure modes, and the fail-closed rule.

The failure tests are the important ones. A search that fails silently - returning
an empty result instead of an error - is exactly how a model ends up answering from
memory while presenting it as researched. Every transport and protocol failure here
must therefore raise, which the tool layer turns into an errored tool result.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from onion_harness.core import EventType, Task
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineToolCall, EngineUsage
from onion_harness.ledger import ChangeLedger, LedgerStore
from onion_harness.plugins import builtin_executors
from onion_harness.plugins.web_search import (
    DEFAULT_BASE_URL,
    TOOL_NAME,
    TOOL_SCHEMA,
    SearchError,
    SearchResponse,
    SearchResult,
    WebSearchClient,
    WebSearchConfig,
    WebSearchTool,
    build_web_search_tool,
    parse_payload,
)
from onion_harness.sandbox import Sandbox


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_json=False, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json
        self.text = text

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """Records the request and replays a canned outcome."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class _TimeoutLike(Exception):
    """Named so the client classifies it as a timeout, like requests' own."""


class ParsePayloadTest(unittest.TestCase):
    def test_reads_answer_and_top(self):
        data = {
            "answer": "  Python 3.13 is the current release.  ",
            "top": [
                {"title": "Python 3.13", "url": "https://python.org/313", "snippet": "Release notes"},
                {"title": "What's new", "url": "https://docs.python.org", "snippet": "Changelog"},
            ],
        }
        result = parse_payload(data, "python version")
        self.assertEqual(result.answer, "Python 3.13 is the current release.")
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.results[0].title, "Python 3.13")
        self.assertEqual(result.results[0].url, "https://python.org/313")
        self.assertEqual(result.results[0].snippet, "Release notes")
        self.assertFalse(result.is_empty)

    def test_missing_fields_become_empty(self):
        result = parse_payload({"top": [{"title": "only a title"}]}, "q")
        self.assertEqual(result.answer, "")
        self.assertEqual(result.results[0].url, "")
        self.assertEqual(result.results[0].snippet, "")

    def test_tolerates_alternate_field_names(self):
        # The service is a separate project; a rename should not break the tool.
        data = {"top": [{"name": "Title", "link": "https://x", "desc": "Description"}]}
        result = parse_payload(data, "q")
        self.assertEqual(result.results[0].title, "Title")
        self.assertEqual(result.results[0].url, "https://x")
        self.assertEqual(result.results[0].snippet, "Description")

    def test_bare_url_strings_are_accepted(self):
        result = parse_payload({"top": ["https://a.example", "https://b.example"]}, "q")
        self.assertEqual([item.url for item in result.results], ["https://a.example", "https://b.example"])

    def test_respects_max_results(self):
        data = {"top": [{"title": f"t{i}"} for i in range(50)]}
        self.assertEqual(len(parse_payload(data, "q", max_results=3).results), 3)

    def test_ignores_junk_entries(self):
        result = parse_payload({"top": [None, 42, {"title": "ok"}]}, "q")
        self.assertEqual(len(result.results), 1)

    def test_non_dict_payload_is_empty(self):
        self.assertTrue(parse_payload([1, 2, 3], "q").is_empty)
        self.assertTrue(parse_payload(None, "q").is_empty)

    def test_long_fields_are_truncated(self):
        result = parse_payload({"answer": "a" * 5000, "top": [{"snippet": "s" * 5000}]}, "q")
        self.assertLessEqual(len(result.answer), 2000)
        self.assertLessEqual(len(result.results[0].snippet), 400)


class ResponseTextTest(unittest.TestCase):
    def test_text_lists_sources(self):
        response = SearchResponse(
            query="q", answer="summary", results=[SearchResult(title="T", url="https://x")]
        )
        text = response.to_text()
        self.assertIn("summary", text)
        self.assertIn("https://x", text)

    def test_empty_result_is_explicit(self):
        text = SearchResponse(query="q").to_text()
        self.assertIn("no results", text.lower())
        # It must not imply the fact does not exist.
        self.assertIn("does not prove", text.lower())

    def test_payload_marks_empty_with_a_note(self):
        payload = SearchResponse(query="q").to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["note"])


class ClientSuccessTest(unittest.TestCase):
    def test_request_shape(self):
        session = _FakeSession(_FakeResponse(payload={"answer": "a", "top": []}))
        client = WebSearchClient(WebSearchConfig(base_url="http://localhost:8080"), session)
        client.search("  多  空格  ")
        call = session.calls[0]
        self.assertEqual(call["url"], "http://localhost:8080/search")
        # The query is normalised, and compact is always 1 for the LLM-facing form.
        self.assertEqual(call["params"], {"q": "多 空格", "compact": 1})
        self.assertEqual(call["timeout"], WebSearchConfig().timeout)

    def test_loopback_bypasses_the_proxy(self):
        session = _FakeSession(_FakeResponse(payload={"top": []}))
        WebSearchClient(WebSearchConfig(base_url=DEFAULT_BASE_URL), session).search("q")
        self.assertEqual(session.calls[0]["proxies"], {"http": None, "https": None})

    def test_remote_host_does_not_force_no_proxy(self):
        session = _FakeSession(_FakeResponse(payload={"top": []}))
        WebSearchClient(WebSearchConfig(base_url="http://search.internal:8080"), session).search("q")
        self.assertIsNone(session.calls[0]["proxies"])

    def test_custom_path(self):
        session = _FakeSession(_FakeResponse(payload={"top": []}))
        config = WebSearchConfig(base_url="http://localhost:9000/", path="api/search")
        WebSearchClient(config, session).search("q")
        self.assertEqual(session.calls[0]["url"], "http://localhost:9000/api/search")


class ClientFailureTest(unittest.TestCase):
    """Every failure must raise, so the tool result is marked as an error."""

    def _client(self, **kwargs):
        return WebSearchClient(WebSearchConfig(), _FakeSession(**kwargs))

    def test_connection_refused(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(error=ConnectionError("refused")).search("q")
        self.assertEqual(ctx.exception.code, "search_unavailable")
        self.assertIn("unavailable", str(ctx.exception))

    def test_read_timeout(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(error=_TimeoutLike("timed out")).search("q")
        self.assertEqual(ctx.exception.code, "search_timeout")
        self.assertIn("did not answer", str(ctx.exception))

    def test_connect_timeout_means_not_running_not_slow(self):
        """ConnectTimeout subclasses both ConnectionError and Timeout.

        Reporting it as a timeout would tell the user "the service is too slow"
        when in fact nothing is listening on that port - the wrong thing to fix.
        """

        class ConnectTimeout(Exception):
            pass

        with self.assertRaises(SearchError) as ctx:
            self._client(error=ConnectTimeout("connect timed out")).search("q")
        self.assertEqual(ctx.exception.code, "search_unavailable")
        self.assertIn("not running", str(ctx.exception))

    def test_500(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(response=_FakeResponse(status_code=500)).search("q")
        self.assertEqual(ctx.exception.code, "search_unavailable")
        self.assertIn("HTTP 500", ctx.exception.detail)

    def test_503(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(response=_FakeResponse(status_code=503)).search("q")
        self.assertEqual(ctx.exception.code, "search_unavailable")

    def test_400_is_a_rejection_not_a_crash(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(response=_FakeResponse(status_code=400)).search("q")
        self.assertEqual(ctx.exception.code, "search_rejected")

    def test_non_json_body(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(response=_FakeResponse(raise_json=True)).search("q")
        self.assertEqual(ctx.exception.code, "bad_response")

    def test_error_inside_a_200_body(self):
        with self.assertRaises(SearchError) as ctx:
            self._client(response=_FakeResponse(payload={"error": "upstream down"})).search("q")
        self.assertEqual(ctx.exception.code, "search_unavailable")
        self.assertIn("upstream down", ctx.exception.detail)

    def test_empty_query_is_rejected_before_any_request(self):
        session = _FakeSession(_FakeResponse(payload={"top": []}))
        client = WebSearchClient(WebSearchConfig(), session)
        with self.assertRaises(SearchError) as ctx:
            client.search("   ")
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertEqual(session.calls, [], "no request should be sent for an empty query")

    def test_error_payload_is_structured(self):
        error = SearchError("boom", code="search_unavailable", detail="d")
        payload = error.to_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "search_unavailable")
        json.dumps(payload)

    def test_messages_tell_the_model_not_to_improvise(self):
        """The wording is load-bearing: it is the anti-hallucination instruction."""
        with self.assertRaises(SearchError) as ctx:
            self._client(error=ConnectionError("refused")).search("q")
        self.assertIn("from memory", str(ctx.exception))


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = WebSearchConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.base_url, DEFAULT_BASE_URL)
        self.assertEqual(config.timeout, 5.0)
        self.assertEqual(config.endpoint(), "http://localhost:8080/search")

    def test_timeout_is_clamped(self):
        self.assertGreaterEqual(WebSearchConfig.from_public({"timeout": 0.01}).timeout, 0.5)
        self.assertLessEqual(WebSearchConfig.from_public({"timeout": 9999}).timeout, 30.0)

    def test_garbage_timeout_falls_back(self):
        self.assertEqual(WebSearchConfig.from_public({"timeout": "abc"}).timeout, 5.0)

    def test_roundtrip(self):
        config = WebSearchConfig(enabled=False, base_url="http://x:1", timeout=9)
        self.assertEqual(
            WebSearchConfig.from_public(config.to_public()).to_public(), config.to_public()
        )

    def test_trailing_slash_is_normalised(self):
        config = WebSearchConfig(base_url="http://localhost:8080/")
        self.assertEqual(config.endpoint(), "http://localhost:8080/search")


class ToolContractTest(unittest.TestCase):
    def test_schema_matches_the_spec(self):
        self.assertEqual(TOOL_NAME, "web_search")
        self.assertEqual(TOOL_SCHEMA["required"], ["query"])
        self.assertEqual(TOOL_SCHEMA["properties"]["query"]["type"], "string")
        self.assertFalse(TOOL_SCHEMA["additionalProperties"])

    def test_tool_carries_search_metadata(self):
        tool = build_web_search_tool(WebSearchConfig())
        self.assertEqual(tool.name, "web_search")
        self.assertEqual(tool.metadata["kind"], "web-search")

    def test_disabled_search_yields_no_tool(self):
        """The model must not be told it can search when it cannot."""
        self.assertIsNone(build_web_search_tool(WebSearchConfig(enabled=False)))

    def test_handler_returns_readable_text(self):
        session = _FakeSession(
            _FakeResponse(payload={"answer": "summary", "top": [{"title": "T", "url": "https://x"}]})
        )
        tool = WebSearchTool(WebSearchConfig(), WebSearchClient(WebSearchConfig(), session))
        text = tool.handler({"query": "hello"})
        self.assertIn("summary", text)
        self.assertIn("https://x", text)

    def test_handler_raises_on_failure(self):
        """Raising is what marks the tool result as failed."""
        tool = WebSearchTool(
            WebSearchConfig(), WebSearchClient(WebSearchConfig(), _FakeSession(error=ConnectionError("x")))
        )
        with self.assertRaises(SearchError):
            tool.handler({"query": "hello"})

    def test_handler_survives_missing_arguments(self):
        tool = WebSearchTool(
            WebSearchConfig(),
            WebSearchClient(WebSearchConfig(), _FakeSession(_FakeResponse(payload={"top": []}))),
        )
        with self.assertRaises(SearchError):
            tool.handler({})


# ------------------------------------------------------------------ runtime
class _FakeProvider:
    def __init__(self, script):
        self.model = "fake"
        self.last_reasoning = ""
        self._script = script
        self._i = 0
        self.tools_seen = []

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        self.tools_seen.append([getattr(t, "name", None) for t in (tools or [])])
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(5, 2, 7),
            model=self.model,
        )


class RuntimeRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-search-"))
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self._original = runtime_module.build_provider
        self.provider = None

    def tearDown(self):
        runtime_module.build_provider = self._original
        self.ledger.close()

    def _run(self, script, tools):
        from onion_harness.harness.runtime import OnionRuntime, build_model_config

        self.provider = _FakeProvider(script)
        runtime_module.build_provider = lambda *a, **k: self.provider
        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            tool_adapters=tools,
            state_provider=lambda: self.ledger.state,
        )
        model = build_model_config({"provider_id": "deepseek", "api_key": "sk-test", "mode": "strict"})
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt="search please", workspace=str(self.dir)), model):
                events.append(ev)

        asyncio.run(consume())
        return events

    def test_search_tool_is_offered_to_the_model(self):
        tool = build_web_search_tool(WebSearchConfig())
        self._run([{"text": "ok"}], [tool])
        self.assertIn("web_search", self.provider.tools_seen[0])

    def test_failed_search_reaches_the_model_as_an_error(self):
        """The whole point of fail-closed: an errored result, never a fake one."""
        class BoomClient:
            config = WebSearchConfig()

            def search(self, query):
                raise SearchError(
                    "The search service is unavailable. Tell the user the search failed "
                    "instead of answering from memory.",
                    code="search_unavailable",
                )

        tool = WebSearchTool(WebSearchConfig(), BoomClient()).as_tool()
        events = self._run(
            [
                {
                    "tool_calls": [
                        EngineToolCall(id="c1", name="web_search", arguments={"query": "news"})
                    ]
                },
                {"text": "the search failed"},
            ],
            [tool],
        )
        results = [ev for ev in events if ev.type is EventType.TOOL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].payload["is_error"])
        self.assertEqual(results[0].payload["status"], "failed")
        self.assertIn("from memory", results[0].payload["content"])

    def test_search_prompt_hint_appears_only_with_the_tool(self):
        self._run([{"text": "ok"}], [build_web_search_tool(WebSearchConfig())])
        # The provider receives the system prompt through the engine; the hint is
        # compiled into it, so check the runtime composer directly instead.
        from onion_harness.harness.runtime import OnionRuntime, SEARCH_PROMPT_HINT

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            tool_adapters=[build_web_search_tool(WebSearchConfig())],
            state_provider=lambda: self.ledger.state,
        )
        prompt = runtime._compose_system_prompt(self.ledger.state, mode="standard")
        self.assertIn("web_search", prompt)
        self.assertIn("do not answer from memory", prompt)
        self.assertIn(SEARCH_PROMPT_HINT.strip()[:40], prompt)

    def test_prompt_hint_absent_when_search_is_off(self):
        from onion_harness.harness.runtime import OnionRuntime

        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            tool_adapters=[],
            state_provider=lambda: self.ledger.state,
        )
        prompt = runtime._compose_system_prompt(self.ledger.state, mode="standard")
        self.assertNotIn("web_search", prompt)


class ServiceRegistrationTest(unittest.TestCase):
    def setUp(self):
        from onion_harness.api.service import OnionService
        import os

        os.environ.setdefault("ONION_SECRET_STORE", "memory")
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-searchsvc-")))
        self.service.start()

    def tearDown(self):
        self.service.stop()

    def test_bootstrap_exposes_the_config(self):
        boot = self.service.bootstrap()
        self.assertIn("web_search", boot)
        self.assertTrue(boot["web_search"]["enabled"])

    def test_tool_is_registered_by_default(self):
        names = [tool.name for tool in self.service._extra_tools()]
        self.assertIn("web_search", names)

    def test_disabling_removes_the_tool_hot(self):
        self.service.update_settings({"web_search": {"enabled": False}})
        names = [tool.name for tool in self.service._extra_tools()]
        self.assertNotIn("web_search", names)
        self.assertFalse(self.service.bootstrap()["web_search"]["enabled"])

    def test_reenabling_restores_it(self):
        self.service.update_settings({"web_search": {"enabled": False}})
        self.service.update_settings({"web_search": {"enabled": True}})
        names = [tool.name for tool in self.service._extra_tools()]
        self.assertIn("web_search", names)

    def test_endpoint_override_is_kept(self):
        updated = self.service.update_settings(
            {"web_search": {"base_url": "http://localhost:9999"}}
        )
        self.assertEqual(updated["web_search"]["base_url"], "http://localhost:9999")
        self.assertEqual(self.service.web_search_config.endpoint(), "http://localhost:9999/search")

    def test_partial_patch_keeps_other_fields(self):
        self.service.update_settings({"web_search": {"timeout": 9}})
        updated = self.service.update_settings({"web_search": {"base_url": "http://localhost:7"}})
        self.assertEqual(updated["web_search"]["timeout"], 9)


if __name__ == "__main__":
    unittest.main()