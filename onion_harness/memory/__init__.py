"""Layer 2 - long-term memory and retrieval.

Sits beside ``ledger``: it depends only on ``core`` and is consumed by the
harness. Keeping it here (rather than inside the harness) lets other layers use
it later without an outward dependency.

Public surface: :class:`MemoryStore` and :class:`Memory`.
"""

from onion_harness.memory.model import Memory, MemoryKind, new_memory
from onion_harness.memory.store import MemoryStore, MemoryStoreError
from onion_harness.memory.extract import MemoryExtractor

__all__ = [
    "Memory",
    "MemoryKind",
    "new_memory",
    "MemoryStore",
    "MemoryStoreError",
    "MemoryExtractor",
]
