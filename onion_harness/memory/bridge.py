"""The port between the harness and the memory store.

The harness must not own a database file, and the store must not know about
tasks or events. This adapter is the seam: the harness asks for memories by
text, hands back a finished event stream to learn from, and gets prompt text.
Everything it can do is bounded and safe to fail, because a memory problem must
never take down a task run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from onion_harness.memory.extract import MemoryExtractor
from onion_harness.memory.model import Memory
from onion_harness.memory.prompt import compose_memory_prompt
from onion_harness.memory.store import MemoryStore, MemoryStoreError

logger = logging.getLogger("onion.memory")


class MemoryBridge:
    """Recall, learn, and compose - the whole memory surface the harness sees."""

    def __init__(
        self,
        store: MemoryStore,
        extractor: Optional[MemoryExtractor] = None,
        recall_limit: int = 6,
    ) -> None:
        self.store = store
        self.extractor = extractor or MemoryExtractor()
        self.recall_limit = recall_limit

    # -- harness-facing -----------------------------------------------------
    def recall(self, query: str) -> List[Memory]:
        """Memories worth putting in front of the model for this request.

        A recall failure returns an empty list: the task then runs with no
        memory rather than not running at all.
        """
        text = (query or "").strip()
        if not text:
            return []
        try:
            memories = self.store.recall(text, limit=self.recall_limit)
        except MemoryStoreError as exc:
            logger.warning("memory recall failed: %s", exc)
            return []
        except Exception:  # noqa: BLE001 - never break a run over memory
            logger.debug("memory recall crashed", exc_info=True)
            return []
        # Recency drives ranking on the next recall, so a memory that was
        # actually useful moves up.
        for memory in memories:
            try:
                self.store.touch(memory.id)
            except Exception:  # noqa: BLE001
                break
        return memories

    def learn(self, events: Iterable[Dict[str, Any]], task_id: str = "") -> List[Memory]:
        """Extract and persist what this task taught us about the user."""
        try:
            found = self.extractor.extract(events, task_id=task_id)
        except Exception:  # noqa: BLE001
            logger.debug("memory extraction crashed", exc_info=True)
            return []
        learned: List[Memory] = []
        for memory in found:
            try:
                self.store.remember(memory)
                learned.append(memory)
            except Exception:  # noqa: BLE001 - one bad row must not stop the rest
                logger.debug("could not store memory", exc_info=True)
        return learned

    def prompt_for(self, memories: Iterable[Memory]) -> str:
        try:
            return compose_memory_prompt(memories)
        except Exception:  # noqa: BLE001
            return ""

    # -- introspection ------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        try:
            return self.store.stats()
        except Exception:  # noqa: BLE001
            return {"total": 0, "by_kind": {}}

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            return [memory.to_dict() for memory in self.store.recent(limit)]
        except Exception:  # noqa: BLE001
            return []

    def forget(self, memory_id: str) -> bool:
        try:
            return self.store.forget(memory_id)
        except Exception:  # noqa: BLE001
            return False

    def update(self, memory_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Edit a memory from the GUI. Returns the stored record, or None."""
        try:
            memory = self.store.update(memory_id, **fields)
        except MemoryStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryStoreError(f"could not update memory: {exc}") from None
        return memory.to_dict() if memory is not None else None

    def add(
        self, text: str, kind: str = "fact", weight: Optional[float] = None
    ) -> Dict[str, Any]:
        """Create a memory directly, for the GUI or the agent's own tool."""
        from onion_harness.memory.model import new_memory

        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            raise MemoryStoreError("memory text cannot be empty")
        memory = new_memory(
            cleaned,
            kind,
            weight=0.8 if weight is None else weight,
        )
        try:
            self.store.remember(memory)
        except Exception as exc:  # noqa: BLE001
            raise MemoryStoreError(f"could not store memory: {exc}") from None
        return memory.to_dict()

    def purge(self) -> int:
        try:
            return self.store.purge()
        except Exception:  # noqa: BLE001
            return 0


__all__ = ["MemoryBridge"]