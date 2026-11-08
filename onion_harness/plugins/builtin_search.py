"""A built-in search backend, so web search works without a separate service.

The tool's contract was designed around an external local service. That is fine when
one is installed and a dead end when it is not - which is the situation this machine
was in: the agent reported "search service is unavailable" with nothing the user
could reasonably do about it.

This module implements the same ``{answer, top}`` shape in-process, so the feature
has no external dependency at all. It queries public result pages directly, which
means it is honest about being fragile: markup changes break parsers, and a network
that blocks both engines makes it as unavailable as any other search. Every failure
is reported as a failure, never as an empty result - the same rule the tool already
follows.

Preference order when both exist: an external service wins, because a purpose-built
aggregator with cross-validation produces better answers than HTML scraping.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger("onion.builtinsearch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 8.0
MAX_RESULTS = 10

# Tags used by the result pages. Kept as module constants so a markup change is one
# edit, not a hunt through regexes.
_BING_RESULT = re.compile(
    r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r".*?(?:<p[^>]*>(?P<snippet>.*?)</p>)?",
    re.DOTALL | re.IGNORECASE,
)
_BAIDU_RESULT = re.compile(
    r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>\s*<a[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(raw: str) -> str:
    """Strip tags and decode entities from a fragment of result HTML."""
    return html.unescape(_TAG_RE.sub("", raw or "")).strip()


@dataclass
class BuiltinSearchResult:
    title: str = ""
    url: str = ""
    snippet: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass
class BuiltinSearchResponse:
    query: str
    answer: str = ""
    results: List[BuiltinSearchResult] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.answer and not self.results

    def to_compact(self) -> Dict[str, Any]:
        """The shape the external service returns, so callers cannot tell them apart."""
        return {
            "answer": self.answer,
            "top": [result.to_dict() for result in self.results],
        }


class BuiltinSearch:
    """In-process search. No subprocess, no port, no extra binary."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT, max_results: int = MAX_RESULTS) -> None:
        self.timeout = timeout
        self.max_results = max(1, min(max_results, MAX_RESULTS))

    def search(self, query: str) -> BuiltinSearchResponse:
        """Query the engines. Raises on total failure, like the external path."""
        clean = " ".join(str(query or "").split())
        if not clean:
            raise ValueError("a query is required")

        errors: List[str] = []
        results: List[BuiltinSearchResult] = []

        for engine in (self._bing, self._baidu):
            try:
                found = engine(clean)
            except Exception as exc:  # noqa: BLE001 - try the next engine
                errors.append(f"{engine.__name__.lstrip('_')}: {type(exc).__name__}")
                continue
            if found:
                results.extend(found)
            if len(results) >= self.max_results:
                break

        if not results:
            # Both engines failed or returned nothing. Distinguishing "blocked" from
            # "no hits" matters: one is fixable, the other is an answer.
            if errors:
                raise RuntimeError("; ".join(errors))
            return BuiltinSearchResponse(query=clean)

        # Deduplicate by host+path, keeping the first (higher ranked) occurrence.
        seen: set[str] = set()
        unique: List[BuiltinSearchResult] = []
        for item in results:
            key = _canonical(item.url)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= self.max_results:
                break

        return BuiltinSearchResponse(
            query=clean,
            # No cross-engine summary: synthesising one from scraped snippets would be
            # presented as a verified answer, and it is not.
            answer="",
            results=unique,
        )

    # -- engines -----------------------------------------------------------
    def _get(self, url: str) -> str:
        import requests

        response = requests.get(
            url,
            timeout=self.timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
            # Loopback is not involved, but a broken system proxy should not turn a
            # working connection into "unavailable" without saying so.
        )
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _bing(self, query: str) -> List[BuiltinSearchResult]:
        page = self._get(f"https://www.bing.com/search?q={quote_plus(query)}&setlang=zh-CN")
        out: List[BuiltinSearchResult] = []
        for match in _BING_RESULT.finditer(page):
            url = html.unescape(match.group("url") or "")
            if not url.startswith("http"):
                continue
            out.append(
                BuiltinSearchResult(
                    title=_text(match.group("title")),
                    url=url,
                    snippet=_text(match.group("snippet") or "")[:400],
                )
            )
            if len(out) >= self.max_results:
                break
        return out

    def _baidu(self, query: str) -> List[BuiltinSearchResult]:
        page = self._get(f"https://www.baidu.com/s?wd={quote_plus(query)}")
        out: List[BuiltinSearchResult] = []
        for match in _BAIDU_RESULT.finditer(page):
            url = html.unescape(match.group("url") or "")
            # Baidu wraps links in a redirect; the real target is in the page, but
            # following it would cost a request per hit for little gain.
            if not url.startswith("http"):
                continue
            out.append(
                BuiltinSearchResult(
                    title=_text(match.group("title")),
                    url=url,
                )
            )
            if len(out) >= self.max_results:
                break
        return out


def _canonical(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if not parsed.netloc:
        return ""
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


__all__ = ["BuiltinSearch", "BuiltinSearchResponse", "BuiltinSearchResult"]