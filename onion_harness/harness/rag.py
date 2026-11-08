"""Retrieval that does more than one lookup.

Plain RAG is one call: search the user's words, paste the top hits, generate. It
fails in a specific, predictable way - the user's phrasing is not the phrasing in
the stored text, so the right note never comes back, and whatever did come back is
pasted in the same order the index returned it.

This module adds the two steps that fix each half:

* **Rewrite** the question into several search probes before retrieving. "Why is
  my build slow" becomes probes about build time, compile flags and caching, which
  finds notes the original words would miss.
* **Rerank** the pooled candidates against the actual question, then keep only the
  best. Retrieval order is not relevance order.

Both steps are optional and both degrade safely: with no model available the
pipeline falls back to the original query and lexical scoring, which is exactly the
previous behaviour. Nothing here may break recall - a failure means "use the plain
path", never "return nothing".

Finally, every surviving snippet is labelled with an id and its source, and the
generation prompt is instructed to cite those ids. An answer that cannot be traced
to a snippet is indistinguishable from a guess.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence

from onion_harness.memory.model import Memory

logger = logging.getLogger("onion.rag")

MAX_QUERIES = 4
MAX_CANDIDATES = 24
MAX_SNIPPETS = 6
MAX_SNIPPET_CHARS = 700

# Words that carry no retrieval weight; used by the lexical fallback scorer.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "for", "with", "and", "or", "but", "if",
    "how", "what", "why", "when", "where", "which", "who", "do", "does", "did",
    "can", "could", "should", "would", "will", "my", "i", "you", "it", "this",
    "that", "there", "then", "than", "so", "not", "no", "yes",
    "的", "了", "是", "在", "我", "你", "他", "它", "和", "与", "就", "也",
    "怎", "么", "为", "什", "哪", "吗", "呢", "吧", "啊", "这", "那",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")


# ------------------------------------------------------------------ snippets
@dataclass
class Snippet:
    """One retrieved passage, carrying the id it will be cited by."""

    id: str
    text: str
    kind: str = "fact"
    source_task: str = ""
    score: float = 0.0
    memory_id: str = ""
    # Which probe surfaced it, for debugging a bad recall.
    query: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "source_task": self.source_task,
            "score": round(self.score, 4),
        }

    def citation(self) -> str:
        label = f" ({self.source_task})" if self.source_task else ""
        return f"[{self.id}]{label}"


@dataclass
class RetrievalResult:
    """Everything the generation prompt needs, plus what happened on the way."""

    question: str
    queries: List[str] = field(default_factory=list)
    snippets: List[Snippet] = field(default_factory=list)
    rewritten: bool = False
    reranked: bool = False
    # Why the pipeline degraded, if it did. Surfaced so a silent quality drop is
    # never invisible.
    degraded: str = ""

    @property
    def empty(self) -> bool:
        return not self.snippets

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "queries": list(self.queries),
            "snippets": [snippet.to_dict() for snippet in self.snippets],
            "rewritten": self.rewritten,
            "reranked": self.reranked,
            "degraded": self.degraded,
        }

    def compose_prompt(self) -> str:
        """The context block handed to the generator, with citation instructions."""
        if not self.snippets:
            return ""
        lines = [
            "## Retrieved context",
            "The notes below were retrieved for this question. Cite them by their "
            "bracketed id when you use them, for example [1]. If none of them answers "
            "the question, say so plainly instead of filling the gap from memory.",
            "",
        ]
        for snippet in self.snippets:
            lines.append(f"{snippet.citation()} {snippet.text.strip()}")
        return "\n".join(lines)


# ------------------------------------------------------------------- scoring
def _tokens(text: str) -> List[str]:
    lowered = (text or "").lower()
    words = [w for w in _WORD_RE.findall(lowered) if w not in _STOPWORDS and len(w) > 1]
    # CJK has no spaces, so bigrams stand in for words.
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            words.append(run)
        else:
            words.extend(run[index : index + 2] for index in range(len(run) - 1))
    return words


def lexical_score(question: str, text: str) -> float:
    """Overlap between the question and a passage, weighted toward long matches.

    Used when no model is available for reranking, and as a tie-breaker when one
    is. Deliberately simple: it only has to beat "index order".
    """
    asked = _tokens(question)
    if not asked:
        return 0.0
    haystack = set(_tokens(text))
    if not haystack:
        return 0.0
    hits = [token for token in asked if token in haystack]
    if not hits:
        return 0.0
    # Longer shared tokens are more informative than short ones.
    weight = sum(min(len(token), 8) for token in hits)
    return weight / (sum(min(len(token), 8) for token in asked) or 1)


# --------------------------------------------------------------- the pipeline
RewriteFn = Callable[[str], Any]           # -> str | list[str] | Awaitable[...]
RerankFn = Callable[[str, List[str]], Any]  # -> list[float] | Awaitable[list[float]]


class RagPipeline:
    """Rewrite, retrieve, rerank, cite.

    ``recall`` is the store-facing callable and is the only required dependency.
    ``rewrite`` and ``rerank`` are optional model hooks; when they raise or return
    something unusable the pipeline falls back rather than failing.
    """

    def __init__(
        self,
        recall: Callable[[str, int], Sequence[Memory]],
        rewrite: Optional[RewriteFn] = None,
        rerank: Optional[RerankFn] = None,
        max_queries: int = MAX_QUERIES,
        max_candidates: int = MAX_CANDIDATES,
        max_snippets: int = MAX_SNIPPETS,
    ) -> None:
        self._recall = recall
        self._rewrite = rewrite
        self._rerank = rerank
        self.max_queries = max(1, min(max_queries, MAX_QUERIES))
        self.max_candidates = max(1, min(max_candidates, MAX_CANDIDATES))
        self.max_snippets = max(1, min(max_snippets, MAX_SNIPPETS))

    async def retrieve(self, question: str) -> RetrievalResult:
        """Full pipeline for one question. Never raises."""
        question = (question or "").strip()
        result = RetrievalResult(question=question)
        if not question:
            return result

        queries = [question]
        if self._rewrite is not None:
            try:
                rewritten = await _maybe_await(self._rewrite(question))
                extra = _normalise_queries(rewritten)
                if extra:
                    # The original always stays first: a rewrite is a hint, not a
                    # replacement, and dropping the user's own words is how a
                    # rewrite makes recall worse.
                    queries = _dedupe([question, *extra])[: self.max_queries]
                    result.rewritten = len(queries) > 1
            except Exception as exc:  # noqa: BLE001 - degrade, never fail
                logger.debug("query rewrite failed", exc_info=True)
                result.degraded = f"query rewrite unavailable: {type(exc).__name__}"

        result.queries = queries
        candidates = self._candidates(queries)
        if not candidates:
            return result

        scored = await self._score(question, candidates, result)
        scored.sort(key=lambda snippet: snippet.score, reverse=True)

        # A snippet that only arrived because a probe returned it loosely, and that
        # neither the reranker nor the lexical check found relevant, is noise. Feeding
        # it to the generator invites the model to cite something unrelated.
        relevant = [snippet for snippet in scored if snippet.score > 0.0]
        if not relevant:
            result.degraded = result.degraded or "no candidate was relevant to the question"
            return result

        result.snippets = relevant[: self.max_snippets]
        # Citation ids are assigned last, so they follow the final order and [1] is
        # always the most relevant snippet rather than whichever the index found
        # first.
        for position, snippet in enumerate(result.snippets, start=1):
            snippet.id = str(position)
        return result

    def _candidates(self, queries: Sequence[str]) -> List[Snippet]:
        """Pool recall results across probes, keeping the best rank per memory."""
        pooled: Dict[str, Snippet] = {}
        per_query = max(4, self.max_candidates // max(1, len(queries)))
        for query in queries:
            try:
                memories = list(self._recall(query, per_query))
            except Exception as exc:  # noqa: BLE001 - one probe may fail
                logger.debug("recall failed for %r: %s", query, exc)
                continue
            for position, memory in enumerate(memories):
                key = getattr(memory, "id", "") or memory.text
                existing = pooled.get(key)
                if existing is not None:
                    continue
                pooled[key] = Snippet(
                    id="",  # assigned after ranking, so citations are stable
                    text=memory.text,
                    kind=memory.kind.value if hasattr(memory.kind, "value") else str(memory.kind),
                    source_task=getattr(memory, "source_task", "") or "",
                    memory_id=getattr(memory, "id", "") or "",
                    # Earlier in a probe's ranking is better.
                    score=1.0 / (1 + position),
                    query=query,
                )
        return list(pooled.values())[: self.max_candidates]

    async def _score(
        self, question: str, candidates: List[Snippet], result: RetrievalResult
    ) -> List[Snippet]:
        """Assign final scores: model rerank when possible, else lexical."""
        lexical = {id(snippet): lexical_score(question, snippet.text) for snippet in candidates}

        if self._rerank is not None:
            if len(candidates) == 1:
                # Nothing to reorder. Probing the model here would cost a call for
                # no possible change in outcome, so it is skipped and said so.
                result.degraded = result.degraded or "rerank skipped: only one candidate"
                for snippet in candidates:
                    snippet.score = lexical[id(snippet)]
                return candidates
            try:
                scores = await _maybe_await(
                    self._rerank(question, [snippet.text for snippet in candidates])
                )
                parsed = _coerce_scores(scores, len(candidates))
                if parsed is not None:
                    for snippet, score in zip(candidates, parsed):
                        # Blend: a model rerank can be overconfident on one snippet,
                        # and lexical agreement is a useful sanity check.
                        snippet.score = 0.7 * score + 0.3 * lexical[id(snippet)]
                    result.reranked = True
                    return candidates
                result.degraded = "reranker returned an unusable score list"
            except Exception as exc:  # noqa: BLE001
                logger.debug("rerank failed", exc_info=True)
                result.degraded = f"reranker unavailable: {type(exc).__name__}"

        for snippet in candidates:
            snippet.score = lexical[id(snippet)]
        return candidates


# ------------------------------------------------------------------ helpers
async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _normalise_queries(raw: Any) -> List[str]:
    """Accept a list, a JSON array, or newline/bullet separated text."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    lines = re.split(r"[\n\r]+|(?:^|\s)[-*]\s+", text)
    return [line.strip(" -•\t") for line in lines if line.strip(" -•\t")]


def _coerce_scores(raw: Any, expected: int) -> Optional[List[float]]:
    """Turn a model reply into one float per candidate, or None."""
    values: Any = raw
    if isinstance(raw, str):
        import json

        stripped = raw.strip()
        try:
            values = json.loads(stripped)
        except json.JSONDecodeError:
            numbers = re.findall(r"-?\d+(?:\.\d+)?", stripped)
            values = [float(number) for number in numbers]
    if not isinstance(values, (list, tuple)):
        return None
    out: List[float] = []
    for item in values:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return None
    if len(out) != expected:
        return None
    # Normalise to 0..1 so the blend with lexical score is meaningful.
    low, high = min(out), max(out)
    if high - low < 1e-9:
        return [1.0 if value == high else 0.0 for value in out]
    return [(value - low) / (high - low) for value in out]


def _dedupe(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value.strip())
    return out


# ---------------------------------------------------------------- prompt text
REWRITE_PROMPT = """Rewrite the user's question into up to 3 short search queries that \
would find the information needed to answer it. Vary the vocabulary: include \
synonyms and the technical terms the answer is likely to use. Reply with a JSON \
array of strings and nothing else.

Question: {question}"""

RERANK_PROMPT = """Score how useful each passage is for answering the question, \
from 0 (irrelevant) to 10 (directly answers it). Reply with a JSON array of {count} \
numbers in the same order as the passages, and nothing else.

Question: {question}

Passages:
{passages}"""


def build_rewrite_prompt(question: str) -> str:
    return REWRITE_PROMPT.format(question=question)


def build_rerank_prompt(question: str, passages: Sequence[str]) -> str:
    listing = "\n\n".join(
        f"[{index + 1}] {text[:MAX_SNIPPET_CHARS]}" for index, text in enumerate(passages)
    )
    return RERANK_PROMPT.format(question=question, count=len(passages), passages=listing)


__all__ = [
    "RagPipeline",
    "RetrievalResult",
    "Snippet",
    "lexical_score",
    "build_rewrite_prompt",
    "build_rerank_prompt",
    "MAX_QUERIES",
    "MAX_SNIPPETS",
]