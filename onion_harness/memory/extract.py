"""Rule-based memory extraction.

Reads a finished task's events and pulls out durable statements. Deliberately
local and deterministic: an extra model call per conversation would double cost
and make extraction untestable. A model-backed summariser can be injected later
through ``summarizer=`` without changing the interface.

What counts as memorable, and why:

* **preferences** - the user states how they want things done. These do not
  decay, because they stay true until the user says otherwise.
* **facts** - durable details about the environment (paths, versions, machine
  layout) that save re-discovery next time.
* **code habits** - a command the user or agent reached for repeatedly.
* **events** - a failure worth remembering, so the same mistake is not repeated.

Everything else is dropped. Recall quality depends more on what is *not* stored
than on what is.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from onion_harness.memory.model import Memory, MemoryKind, new_memory

# A stated preference: "以后都用 X", "always use Y", "prefer Z", "不要用 W".
_PREFERENCE_PATTERNS = [
    re.compile(r"(?:以后|今后|下次|always|from now on)\s*(?:都|请)?\s*(?:用|使用|use)\s*([^\n。.;]{1,60})", re.I),
    re.compile(r"(?:我)?(?:更)?(?:喜欢|偏好|prefer)\s*([^\n。.;]{1,60})", re.I),
    re.compile(r"(?:不要|别|don'?t|never)\s*(?:再)?\s*(?:用|使用|use)\s*([^\n。.;]{1,60})", re.I),
]

# A durable environment fact.
_FACT_PATTERNS = [
    # "项目在 D:/work 下面" puts filler between the marker and the path, so the
    # gap allows any non-path characters rather than only a fixed connector.
    re.compile(
        r"(?:项目|仓库|代码|工作目录|root|repo)[^A-Za-z:\n]{0,12}([A-Za-z]:[\\/][^\s，。；,;]{2,120})",
        re.I,
    ),
    re.compile(r"(?:Python|Node|PowerShell|版本|version)\s*[:：]?\s*(\d+(?:\.\d+)+)", re.I),
]

# A failure worth not repeating.
_ERROR_RE = re.compile(
    r"(?:Error|ERROR|Exception|Traceback|错误|失败)[:：]?\s*([^\n]{4,160})"
)

_MIN_LENGTH = 6
_MAX_LENGTH = 240


class MemoryExtractor:
    """Turns task events into memories.

    ``summarizer`` is an optional callable ``(list[Memory]) -> list[Memory]`` that
    can post-process or enrich the rule output; it exists so a model-backed pass
    can be added without touching callers.
    """

    def __init__(self, summarizer: Optional[Any] = None) -> None:
        self._summarizer = summarizer

    def extract(
        self, events: Iterable[Dict[str, Any]], task_id: str = ""
    ) -> List[Memory]:
        """Derive memories from one task's event stream.

        Duplicates within the same task collapse; the store merges across tasks.
        """
        found: List[Memory] = []
        seen: set[tuple[str, str]] = set()

        def add(text: str, kind: MemoryKind, weight: float) -> None:
            cleaned = _clean(text)
            if not (_MIN_LENGTH <= len(cleaned) <= _MAX_LENGTH):
                return
            key = (kind.value, cleaned)
            if key in seen:
                return
            seen.add(key)
            found.append(
                new_memory(cleaned, kind, weight=weight, source_task=task_id)
            )

        user_texts: List[str] = []
        error_texts: List[str] = []
        executor_counts: Dict[str, int] = {}

        for event in events:
            kind = event.get("type")
            payload = event.get("payload") or {}

            if kind == "user-input":
                user_texts.append(str(payload.get("text") or ""))

            elif kind == "tool-call":
                executor = str(payload.get("executor") or "").strip()
                if executor:
                    executor_counts[executor] = executor_counts.get(executor, 0) + 1

            elif kind == "tool-result":
                if payload.get("is_error"):
                    error_texts.append(str(payload.get("content") or ""))

        for text in user_texts:
            for pattern in _PREFERENCE_PATTERNS:
                for match in pattern.finditer(text):
                    add(
                        f"用户偏好：{match.group(0).strip()}",
                        MemoryKind.PREFERENCE,
                        weight=0.9,
                    )
            for pattern in _FACT_PATTERNS:
                for match in pattern.finditer(text):
                    add(
                        f"环境：{match.group(0).strip()}",
                        MemoryKind.FACT,
                        weight=0.7,
                    )

        for text in error_texts:
            match = _ERROR_RE.search(text)
            if match:
                add(f"此前失败：{match.group(1).strip()}", MemoryKind.EVENT, weight=0.55)

        # A runtime reached for repeatedly is a working habit worth recording.
        for executor, count in executor_counts.items():
            if count >= 2:
                add(
                    f"常用执行器：{executor}（本任务使用 {count} 次）",
                    MemoryKind.CODE_HABIT,
                    weight=0.6,
                )

        if self._summarizer is not None:
            try:
                found = list(self._summarizer(found))
            except Exception:  # noqa: BLE001 - enrichment must never break a run
                pass
        return found


def _clean(text: str) -> str:
    flat = " ".join(str(text or "").split())
    return flat.strip(" ，。；,;:：")


__all__ = ["MemoryExtractor"]