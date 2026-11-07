"""SQLite-backed persistence for the change ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from onion_harness.ledger.model import ChangeRecord, Revision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    version     INTEGER PRIMARY KEY,
    change_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json  TEXT NOT NULL,
    applied_at  REAL NOT NULL,
    reverted_at REAL,
    redo_of     INTEGER,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    change_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class LedgerStore:
    """Thread-safe key/value + revision store on top of SQLite.

    Deliberately tiny: the ledger only needs append-only revisions, staged
    proposals and a materialised settings blob.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- proposals ----------------------------------------------------------
    def save_proposal(self, record: ChangeRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO proposals (id, change_json, created_at) VALUES (?,?,?)",
                (record.id, _dump(record.to_dict()), record.created_at),
            )
            self._conn.commit()

    def delete_proposal(self, change_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM proposals WHERE id = ?", (change_id,))
            self._conn.commit()

    def load_proposals(self) -> List[ChangeRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT change_json FROM proposals ORDER BY created_at ASC"
            ).fetchall()
        out: List[ChangeRecord] = []
        for row in rows:
            raw = _load(row["change_json"])
            out.append(ChangeRecord(**_record_kwargs(raw)))
        return out

    def get_proposal(self, change_id: str) -> Optional[ChangeRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT change_json FROM proposals WHERE id = ?", (change_id,)
            ).fetchone()
        if row is None:
            return None
        return ChangeRecord(**_record_kwargs(_load(row["change_json"])))

    # -- revisions ----------------------------------------------------------
    def next_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(version) AS v FROM revisions").fetchone()
        return int(row["v"] or 0) + 1

    def append_revision(self, revision: Revision) -> None:
        record = revision.change
        with self._lock:
            self._conn.execute(
                "INSERT INTO revisions (version, change_json, before_json, after_json, "
                "applied_at, reverted_at, redo_of, note) VALUES (?,?,?,?,?,?,?,?)",
                (
                    revision.version,
                    _dump(record.to_dict()),
                    _dump(revision.before),
                    _dump(revision.after),
                    revision.applied_at,
                    revision.reverted_at,
                    revision.redo_of,
                    revision.note,
                ),
            )
            self._conn.commit()

    def update_revision(self, revision: Revision) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE revisions SET reverted_at = ?, note = ?, after_json = ? WHERE version = ?",
                (
                    revision.reverted_at,
                    revision.note,
                    _dump(revision.after),
                    revision.version,
                ),
            )
            self._conn.commit()

    def load_revisions(self) -> List[Revision]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM revisions ORDER BY version ASC"
            ).fetchall()
        out: List[Revision] = []
        for row in rows:
            raw = _load(row["change_json"])
            record = ChangeRecord(**_record_kwargs(raw))
            out.append(
                Revision(
                    version=int(row["version"]),
                    change=record,
                    before=_load(row["before_json"]),
                    after=_load(row["after_json"]),
                    applied_at=float(row["applied_at"]),
                    reverted_at=row["reverted_at"],
                    redo_of=row["redo_of"],
                    note=row["note"] or "",
                )
            )
        return out

    # -- materialised state -------------------------------------------------
    def save_state(self, state: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO state (key, value_json) VALUES ('settings', ?)",
                (_dump(state),),
            )
            self._conn.commit()

    def load_state(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM state WHERE key = 'settings'"
            ).fetchone()
        return _load(row["value_json"]) if row else {}

    # -- misc ---------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )
            self._conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(raw: str) -> Any:
    return json.loads(raw)


def _record_kwargs(raw: Dict[str, Any]) -> Dict[str, Any]:
    from onion_harness.ledger.model import ChangeKind, ChangeStatus, DiffHunk

    kwargs = dict(raw)
    kwargs["kind"] = ChangeKind(kwargs["kind"])
    kwargs["status"] = ChangeStatus(kwargs.get("status", "proposed"))
    kwargs.pop("label", None)
    kwargs["diff"] = [
        DiffHunk(path=h["path"], before=h.get("before"), after=h.get("after"))
        for h in kwargs.get("diff", [])
    ]
    return kwargs


__all__ = ["LedgerStore"]