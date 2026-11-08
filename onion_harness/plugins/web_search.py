"""Web search through a local search-neo service.

The agent gets live information without shipping a cloud API key: search-neo runs
on this machine, aggregates Baidu and Bing, and returns one compact JSON document.
Keeping it local means no credential can be extracted from the binary, and it works
behind networks that cloud search APIs cannot reach.

Two design decisions matter more than the parsing.

**Fail closed.** If the service is down, slow, or answering 5xx, the tool raises.
The engine turns that into an errored tool result, so the model is told "the search
failed" instead of receiving an empty result it might treat as "the answer is
nothing". A silent empty result is the single most reliable way to make a model
hallucinate: it will fill the gap from memory and present it as if searched.

**Nothing here may crash the harness.** Every transport failure is caught and
re-raised as one typed error carrying a structured payload; the tool never lets a
``ConnectionError`` or a malformed body escape as an unhandled crash.

The tool is not run through the approval or review path. Review exists to gate
*code execution* before the sandbox; a search is a read-only network call the user
has already enabled, and putting a confirmation dialog in front of every lookup
would make the feature unusable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from onion_harness.core.tool import OnionTool

logger = logging.getLogger("onion.websearch")

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = 5.0
DEFAULT_PATH = "/search"
MAX_RESULTS = 10
MAX_SNIPPET = 400
MAX_ANSWER = 2000

# Hosts that must never go through a proxy. This machine can have a system proxy
# configured (a dead one is common), and sending loopback traffic through it makes
# a working local service look unreachable.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class SearchError(Exception):
    """A search could not be performed. The message is shown to the model.

    Deliberately an exception rather than a return value: the tool layer marks a
    raised error as a failed tool result, which is what tells the model not to
    invent an answer. A returned ``{"error": ...}`` would arrive as a successful
    result and be easy to mistake for data.
    """

    def __init__(self, message: str, *, code: str = "search_failed", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": str(self), "code": self.code, "detail": self.detail}


@dataclass
class SearchResult:
    """One cleaned hit."""

    title: str = ""
    url: str = ""
    snippet: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass
class SearchResponse:
    """The cleaned document handed to the model."""

    query: str
    answer: str = ""
    results: List[SearchResult] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.answer and not self.results

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "query": self.query,
            "answer": self.answer,
            "count": len(self.results),
            "results": [item.to_dict() for item in self.results],
            # Spelled out so the model cannot read "no results" as "no such thing".
            "note": (
                ""
                if not self.is_empty
                else "The search returned no results. This means nothing was found; "
                "it does not prove the information does not exist. Tell the user the "
                "search came back empty rather than answering from memory."
            ),
        }

    def to_text(self) -> str:
        """Compact rendering for the model: answer first, then ranked hits."""
        lines: List[str] = [f"Search results for: {self.query}"]
        if self.answer:
            lines.append("")
            lines.append(f"Summary (cross-validated across engines):\n{self.answer}")
        if self.results:
            lines.append("")
            lines.append("Sources:")
            for index, item in enumerate(self.results, start=1):
                lines.append(f"{index}. {item.title or '(untitled)'}")
                if item.url:
                    lines.append(f"   {item.url}")
                if item.snippet:
                    lines.append(f"   {item.snippet}")
        if self.is_empty:
            payload = self.to_dict()
            lines.append("")
            lines.append(payload["note"])
        return "\n".join(lines)


@dataclass
class WebSearchConfig:
    """Where the service lives and how long to wait for it."""

    enabled: bool = True
    base_url: str = DEFAULT_BASE_URL
    path: str = DEFAULT_PATH
    timeout: float = DEFAULT_TIMEOUT
    max_results: int = MAX_RESULTS

    def endpoint(self) -> str:
        base = (self.base_url or DEFAULT_BASE_URL).rstrip("/")
        path = self.path if (self.path or "").startswith("/") else f"/{self.path or 'search'}"
        return f"{base}{path}"

    def to_public(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_results": self.max_results,
        }

    @classmethod
    def from_public(cls, raw: Any) -> "WebSearchConfig":
        base = cls()
        if not isinstance(raw, dict):
            return base
        try:
            timeout = float(raw.get("timeout", base.timeout))
        except (TypeError, ValueError):
            timeout = base.timeout
        try:
            max_results = int(raw.get("max_results", base.max_results))
        except (TypeError, ValueError):
            max_results = base.max_results
        return cls(
            enabled=bool(raw.get("enabled", base.enabled)),
            base_url=str(raw.get("base_url") or base.base_url),
            path=str(raw.get("path") or base.path),
            # A short ceiling on purpose: the model is mid-turn and blocking on a
            # hung socket stalls the whole run.
            timeout=max(0.5, min(timeout, 30.0)),
            max_results=max(1, min(max_results, MAX_RESULTS)),
        )


# ------------------------------------------------------------------- parsing
def parse_payload(data: Any, query: str, max_results: int = MAX_RESULTS) -> SearchResponse:
    """Clean a search-neo response into the shape the model should see.

    Tolerant about shape: the service is a separate project and its field names
    may drift, so a missing key yields an empty value rather than a crash. Only the
    two documented fields, ``answer`` and ``top``, are read.
    """
    if not isinstance(data, dict):
        return SearchResponse(query=query)

    answer = _as_text(data.get("answer"))
    if len(answer) > MAX_ANSWER:
        answer = answer[: MAX_ANSWER - 1] + "…"

    raw_top = data.get("top")
    results: List[SearchResult] = []
    if isinstance(raw_top, list):
        for entry in raw_top[:max_results]:
            if isinstance(entry, str):
                # Some builds return bare URL strings.
                results.append(SearchResult(url=entry.strip()))
                continue
            if not isinstance(entry, dict):
                continue
            snippet = _as_text(entry.get("snippet") or entry.get("content") or entry.get("desc"))
            if len(snippet) > MAX_SNIPPET:
                snippet = snippet[: MAX_SNIPPET - 1] + "…"
            results.append(
                SearchResult(
                    title=_as_text(entry.get("title") or entry.get("name")),
                    url=_as_text(entry.get("url") or entry.get("link")),
                    snippet=snippet,
                )
            )
    return SearchResponse(query=query, answer=answer, results=results)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _is_loopback(endpoint: str) -> bool:
    try:
        host = (urlparse(endpoint).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


# -------------------------------------------------------------------- client
class WebSearchClient:
    """Synchronous client for the local search service.

    Synchronous because the engine invokes tool handlers inside its own loop and
    accepts blocking handlers; a five-second ceiling keeps that acceptable.
    """

    def __init__(
        self,
        config: Optional[WebSearchConfig] = None,
        session=None,
        builtin=None,
    ) -> None:
        self.config = config or WebSearchConfig()
        self._session = session
        # Used when no external service answers. ``None`` disables the fallback,
        # which is what the tests exercise to check the fail-closed path in
        # isolation.
        self._builtin = builtin

    def _get_session(self):
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        return self._session

    def search(self, query: str) -> SearchResponse:
        """Run one search. Raises :class:`SearchError` on any failure.

        Order: an external service if it answers, otherwise the built-in backend.
        The fallback exists because reporting "unavailable" when Onion could simply
        search for itself is a worse outcome than a slightly noisier result set.
        """
        clean = " ".join(str(query or "").split())
        if not clean:
            raise SearchError(
                "web_search was called without a query. Provide search keywords.",
                code="bad_request",
            )

        try:
            return self._search_external(clean)
        except SearchError as exc:
            if self._builtin is None:
                raise
            logger.info("external search unavailable (%s); using the built-in backend", exc.code)
            return self._search_builtin(clean, exc)

    def _search_builtin(self, clean: str, first_error: SearchError) -> SearchResponse:
        try:
            raw = self._builtin.search(clean)
        except Exception as exc:  # noqa: BLE001 - both paths failed
            raise SearchError(
                "The search service is unavailable and the built-in search could not "
                "reach the web either. Tell the user the search failed instead of "
                "answering from memory.",
                code="search_unavailable",
                detail=f"external: {first_error.detail or first_error}; builtin: {exc}",
            ) from None
        return parse_payload(raw.to_compact(), clean, self.config.max_results)

    def _search_external(self, clean: str) -> SearchResponse:

        endpoint = self.config.endpoint()
        params = {"q": clean, "compact": 1}
        # A dead system proxy is common on this setup, and loopback traffic must
        # not be routed through it or a healthy local service reads as down.
        proxies = {"http": None, "https": None} if _is_loopback(endpoint) else None

        try:
            session = self._get_session()
            response = session.get(
                endpoint,
                params=params,
                timeout=self.config.timeout,
                proxies=proxies,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:  # noqa: BLE001 - normalise every transport failure
            raise self._unavailable(exc) from None

        status = getattr(response, "status_code", 0)
        if status >= 500:
            raise SearchError(
                "The search service is unavailable (it returned a server error). "
                "Tell the user the search failed instead of answering from memory.",
                code="search_unavailable",
                detail=f"HTTP {status}",
            )
        if status >= 400:
            raise SearchError(
                f"The search service rejected the request (HTTP {status}). Check the "
                "query, or whether the service is configured correctly.",
                code="search_rejected",
                detail=f"HTTP {status}",
            )
        if status == 0:
            raise SearchError(
                "The search service is unavailable (no HTTP response). Tell the user "
                "the search failed instead of answering from memory.",
                code="search_unavailable",
            )

        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - a body that is not JSON
            raise SearchError(
                "The search service returned a response this tool cannot read. Tell the "
                "user the search failed instead of answering from memory.",
                code="bad_response",
            ) from None

        # Some services report failure inside a 200 body.
        if isinstance(data, dict) and data.get("error"):
            raise SearchError(
                "The search service reported an error. Tell the user the search failed "
                "instead of answering from memory.",
                code="search_unavailable",
                detail=_as_text(data.get("error"))[:300],
            )

        return parse_payload(data, clean, self.config.max_results)

    def _unavailable(self, exc: BaseException) -> SearchError:
        reason = type(exc).__name__
        # ConnectTimeout inherits from both ConnectionError and Timeout, so a name
        # check alone would report "the service was too slow" when in fact nothing
        # was listening. Those are different problems for the user to fix, so the
        # connect phase is distinguished before the timeout case.
        if "ConnectTimeout" in reason or "ConnectionError" in reason or "NewConnectionError" in reason:
            return SearchError(
                "The search service is unavailable. It is probably not running. Tell the "
                "user the search failed instead of answering from memory.",
                code="search_unavailable",
                detail=f"{reason}: {str(exc)[:280]}",
            )
        if "Timeout" in reason:
            return SearchError(
                f"The search service did not answer within {self.config.timeout:g}s. "
                "Tell the user the search timed out instead of answering from memory.",
                code="search_timeout",
                detail=str(exc)[:300],
            )
        return SearchError(
            "The search service is unavailable. It is probably not running. Tell the "
            "user the search failed instead of answering from memory.",
            code="search_unavailable",
            detail=f"{reason}: {str(exc)[:280]}",
        )

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------- tool
TOOL_NAME = "web_search"
TOOL_DESCRIPTION = (
    "当需要获取实时信息、最新新闻、文档资料或外部知识时调用此工具。"
    " It searches the live web (aggregating Baidu and Bing) and returns a "
    "cross-validated summary plus ranked sources. Use it whenever the answer "
    "depends on facts that change over time, or on documentation you do not have. "
    "If the search fails the tool reports an error: tell the user the search "
    "failed rather than answering from memory."
)
TOOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词",
        }
    },
    "required": ["query"],
    "additionalProperties": False,
}


class WebSearchTool:
    """Binds a client to the ``web_search`` tool contract."""

    def __init__(
        self,
        config: Optional[WebSearchConfig] = None,
        client: Optional[WebSearchClient] = None,
        builtin=None,
    ) -> None:
        self.config = config or (client.config if client is not None else WebSearchConfig())
        self.client = client or WebSearchClient(self.config, builtin=builtin)

    def handler(self, args: Dict[str, Any]) -> str:
        """Never returns an error value: failures raise, which marks the call failed."""
        query = str((args or {}).get("query") or "").strip()
        response = self.client.search(query)
        return response.to_text()

    def as_tool(self) -> OnionTool:
        return OnionTool(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            input_schema=TOOL_SCHEMA,
            handler=self.handler,
            metadata={
                "kind": "web-search",
                "label": "Web search",
                "icon": "globe",
            },
        )


def build_web_search_tool(
    config: Optional[WebSearchConfig] = None,
    client: Optional[WebSearchClient] = None,
    builtin: Any = None,
) -> Optional[OnionTool]:
    """The tool, or ``None`` when search is switched off.

    Returning ``None`` rather than a tool that always fails is deliberate: the
    model must not be told it can search when it cannot.
    """
    config = config or WebSearchConfig()
    if not config.enabled:
        return None
    # The built-in backend is on by default: web search should work on a fresh
    # install with no extra service to find and install.
    if client is None and builtin is None:
        from onion_harness.plugins.builtin_search import BuiltinSearch

        builtin = BuiltinSearch(timeout=config.timeout, max_results=config.max_results)
    return WebSearchTool(config, client, builtin=builtin).as_tool()


__all__ = [
    "SearchError",
    "SearchResponse",
    "SearchResult",
    "WebSearchClient",
    "WebSearchConfig",
    "WebSearchTool",
    "build_web_search_tool",
    "parse_payload",
    "TOOL_NAME",
    "TOOL_DESCRIPTION",
    "TOOL_SCHEMA",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
]