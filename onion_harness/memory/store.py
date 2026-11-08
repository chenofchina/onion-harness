"""Encrypted SQLite storage for memories, with FTS5 retrieval.

Two tables carry the data:

* ``memories`` holds the ciphertext plus ranking metadata.
* ``memories_fts`` is an FTS5 index over *hashed* tokens (a blind index), which
  is what makes Chinese substring search work without writing the words
  themselves to disk (see :mod:`tokenizer`).

Encryption is AES-256-GCM. The key lives in the OS credential store; the
database itself only ever contains ciphertext and keyed digests. A caller
without a key can still open the file and will get nothing readable out of it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from onion_harness.memory.model import Memory, MemoryKind
from onion_harness.memory.tokenizer import build_match_expression, index_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    ciphertext    BLOB NOT NULL,
    weight        REAL NOT NULL,
    source_task   TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    last_used_at  REAL NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS memories_used ON memories(last_used_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


class MemoryStoreError(RuntimeError):
    """Storage-level failure, safe to surface to the UI."""


class MemoryStore:
    """Thread-safe memory storage.

    ``cipher`` is any object exposing ``encrypt(str) -> bytes`` and
    ``decrypt(bytes) -> str``. Passing ``None`` stores plaintext and is only
    appropriate for tests; the application always supplies a real cipher.
    """

    def __init__(self, path: str | Path, cipher: Optional[Any] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = cipher
        # The search index stores token digests rather than tokens, so the file
        # holds no readable text. A cipher without ``token_hash`` (the test
        # doubles) yields an unhashed index, which is fine because those stores
        # are explicitly plaintext.
        self._hasher = getattr(cipher, "token_hash", None)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes -------------------------------------------------------------
    def remember(self, memory: Memory) -> str:
        """Store one memory. Duplicate text of the same kind is merged."""
        existing = self._find_duplicate(memory)
        if existing is not None:
            existing.touch()
            existing.weight = max(existing.weight, memory.weight)
            self._update_meta(existing)
            return existing.id

        payload = self._encode(memory.text)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memories "
                "(id, kind, ciphertext, weight, source_task, created_at, last_used_at, use_count) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    memory.id,
                    memory.kind.value,
                    payload,
                    memory.weight,
                    memory.source_task,
                    memory.created_at,
                    memory.last_used_at,
                    memory.use_count,
                ),
            )
            self._conn.execute(
                "INSERT INTO memories_fts (memory_id, body) VALUES (?, ?)",
                (memory.id, index_text(memory.text, self._hasher)),
            )
            self._conn.commit()
        memory.stored = True
        return memory.id

    def remember_many(self, memories: Iterable[Memory]) -> List[str]:
        return [self.remember(memory) for memory in memories]

    def forget(self, memory_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    def get(self, memory_id: str) -> Optional[Memory]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._from_row(row)
        except Exception:  # noqa: BLE001 - unreadable row (wrong key)
            return None

    def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        kind: Optional[str] = None,
        weight: Optional[float] = None,
    ) -> Optional[Memory]:
        """Edit a memory in place.

        The search index is rebuilt for the row, because editing without
        re-indexing would leave the old wording findable and the new wording
        invisible - the most confusing possible outcome of an edit.
        """
        existing = self.get(memory_id)
        if existing is None:
            return None

        if text is not None:
            cleaned = " ".join(str(text).split())
            if not cleaned:
                raise MemoryStoreError("memory text cannot be empty")
            existing.text = cleaned
        if kind is not None:
            try:
                existing.kind = MemoryKind(str(kind))
            except ValueError:
                raise MemoryStoreError(f"unknown memory kind: {kind}") from None
        if weight is not None:
            existing.weight = max(0.0, min(1.0, float(weight)))

        payload = self._encode(existing.text)
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET kind = ?, ciphertext = ?, weight = ? WHERE id = ?",
                (existing.kind.value, payload, existing.weight, memory_id),
            )
            self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self._conn.execute(
                "INSERT INTO memories_fts (memory_id, body) VALUES (?, ?)",
                (memory_id, index_text(existing.text, self._hasher)),
            )
            self._conn.commit()
        existing.stored = True
        return existing

    def touch(self, memory_id: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()

    def purge(self) -> int:
        """Delete every memory. Returns how many were removed."""
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            self._conn.execute("DELETE FROM memories")
            self._conn.execute("DELETE FROM memories_fts")
            self._conn.commit()
        return int(count)

    # -- reads --------------------------------------------------------------
    def recall(self, query: str, limit: int = 6, kinds: Optional[List[str]] = None) -> List[Memory]:
        """Ranked retrieval for one query.

        Candidates come from FTS5 (crude recall), then are re-ranked by
        ``bm25 × time-decayed weight`` so a strong lexical match on a stale event
        does not outrank a weak match on something the user still cares about.
        """
        expression = build_match_expression(query, self._hasher)
        if not expression:
            return []

        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.*, bm25(memories_fts) AS score "
                    "FROM memories_fts JOIN memories m ON m.id = memories_fts.memory_id "
                    "WHERE memories_fts MATCH ? ORDER BY score LIMIT ?",
                    (expression, max(limit * 8, 32)),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise MemoryStoreError(f"memory search failed: {exc}") from exc

        scored: List[tuple[float, Memory]] = []
        now = time.time()
        for row in rows:
            memory = self._from_row(row)
            if kinds and memory.kind.value not in kinds:
                continue
            # bm25 returns a negative score where more negative is better; map it
            # onto a positive relevance so it can be multiplied by weight.
            relevance = 1.0 / (1.0 + abs(float(row["score"] or 0.0)))
            scored.append((relevance * memory.effective_weight(now), memory))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [memory for _score, memory in scored[:limit]]

    def recent(self, limit: int = 50, kinds: Optional[List[str]] = None) -> List[Memory]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories ORDER BY last_used_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out: List[Memory] = []
        for row in rows:
            memory = self._from_row(row)
            if kinds and memory.kind.value not in kinds:
                continue
            out.append(memory)
        return out

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind"
            ).fetchall()
        return {
            "total": self.count(),
            "by_kind": {row["kind"]: int(row["n"]) for row in rows},
        }

    # -- internals ----------------------------------------------------------
    def _encode(self, text: str) -> bytes:
        if self._cipher is None:
            return text.encode("utf-8")
        return self._cipher.encrypt(text)

    def _decode(self, raw: bytes) -> str:
        if self._cipher is None:
            return bytes(raw).decode("utf-8", errors="replace")
        return self._cipher.decrypt(bytes(raw))

    def _from_row(self, row: sqlite3.Row) -> Memory:
        memory = Memory(
            text=self._decode(row["ciphertext"]),
            kind=MemoryKind(row["kind"]),
            id=row["id"],
            weight=float(row["weight"]),
            source_task=row["source_task"] or "",
            created_at=float(row["created_at"]),
            last_used_at=float(row["last_used_at"]),
            use_count=int(row["use_count"]),
        )
        memory.stored = True
        return memory

    def _find_duplicate(self, memory: Memory) -> Optional[Memory]:
        """Same kind and same text means the same memory; merge instead of adding."""
        needle = memory.text.strip()
        if not needle:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE kind = ?", (memory.kind.value,)
            ).fetchall()
        for row in rows:
            existing = self._from_row(row)
            if existing.text.strip() == needle:
                return existing
        return None

    def _update_meta(self, memory: Memory) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_used_at = ?, use_count = ?, weight = ? WHERE id = ?",
                (memory.last_used_at, memory.use_count, memory.weight, memory.id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["MemoryStore", "MemoryStoreError"]