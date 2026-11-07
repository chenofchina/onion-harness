"""ChangeLedger - the single gate for all self-modification.

Contract::

    propose(change) -> change_id
    apply(change_id) -> revision
    revert(version)  -> list[revision]
    redo()           -> revision | None
    list_history()   -> list[revision]

Guarantees:

* **Nothing persists until a user confirms.** ``propose`` only stages a record
  (kept in SQLite as a pending proposal so a restart does not lose it).
* **Every apply is versioned** and stored with a full before/after snapshot,
  independent of the conversation lifetime.
* **Revert is chain-based.** Reverting version *N* also reverts everything
  applied after *N*, newest first. Reverted revisions are retained, so a
  single ``redo`` can re-apply them in order.
* **Irreversible operations** (external side effects) carry
  ``reversible=False``; they may only be applied when the caller passes the
  explicit acknowledgement flag.
* Every transition is reported through an ``emit`` callback so the API layer
  can mirror it into the event stream.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from onion_harness.ledger.model import (
    ChangeKind,
    ChangeRecord,
    ChangeStatus,
    DiffHunk,
    Revision,
)
from onion_harness.ledger.state import (
    ChangeRejected,
    apply_to_state,
    normalise_state,
    validate,
)
from onion_harness.ledger.store import LedgerStore

EmitFn = Callable[[str, Dict[str, Any]], None]

IRREVERSIBLE_KINDS = {
    # Registering a provider that hits a remote endpoint is reversible locally,
    # but mounting an external skill may touch third-party state.
    ChangeKind.UPDATE_SKILL_REGISTRY,
}


class LedgerError(RuntimeError):
    """Raised when a ledger operation is not permitted."""


class ChangeLedger:
    def __init__(self, store: LedgerStore, emit: Optional[EmitFn] = None) -> None:
        self._store = store
        self._emit = emit or (lambda _kind, _payload: None)
        self._state = normalise_state(store.load_state())
        self._proposals: Dict[str, ChangeRecord] = {}
        self._revisions: List[Revision] = store.load_revisions()

        for record in store.load_proposals():
            record.status = ChangeStatus.PROPOSED
            self._proposals[record.id] = record

        self._initialise_state()

    # -- lifecycle ----------------------------------------------------------
    def _initialise_state(self) -> None:
        if not self._store.load_state():
            self._store.save_state(self._state)
        self._store.set_meta("schema_version", "1")

    @property
    def state(self) -> Dict[str, Any]:
        return self._state

    # -- proposal -----------------------------------------------------------
    def propose(self, change: ChangeRecord) -> str:
        """Stage a change. Does **not** mutate live state."""
        if change.id in self._proposals:
            raise LedgerError(f"duplicate change id {change.id}")

        record = change
        record.status = ChangeStatus.PROPOSED
        record.created_at = record.created_at or time.time()

        try:
            validate(record, self._state)
        except ChangeRejected as exc:
            raise LedgerError(str(exc)) from exc

        # Compute the diff from the materialised state so the GUI can show a
        # truthful preview rather than echoing model claims.
        try:
            after_state = apply_to_state(record, self._state)
        except ChangeRejected as exc:
            raise LedgerError(str(exc)) from exc

        record.diff = _build_diff(record, self._state, after_state)
        record.impact = record.impact or _default_impact(record)
        if record.kind in IRREVERSIBLE_KINDS:
            record.reversible = False
            record.requires_ack = True

        self._proposals[record.id] = record
        self._store.save_proposal(record)
        self._emit(
            "change-proposal",
            {
                "change": record.to_dict(),
                "preview_state": after_state,
            },
        )
        return record.id

    def get_proposal(self, change_id: str) -> Optional[ChangeRecord]:
        return self._proposals.get(change_id)

    def list_proposals(self) -> List[ChangeRecord]:
        return [
            p for p in self._proposals.values() if p.status is ChangeStatus.PROPOSED
        ]

    def discard(self, change_id: str, reason: str = "") -> ChangeRecord:
        record = self._proposals.get(change_id)
        if record is None:
            raise LedgerError(f"unknown change {change_id}")
        if record.status is not ChangeStatus.PROPOSED:
            raise LedgerError(f"change {change_id} is not pending")
        record.status = ChangeStatus.REJECTED
        self._store.delete_proposal(change_id)
        self._proposals.pop(change_id, None)
        self._emit(
            "change-rejected",
            {"change": record.to_dict(), "reason": reason or "rejected by user"},
        )
        return record

    # -- apply --------------------------------------------------------------
    def apply(
        self,
        change_id: str,
        *,
        override_params: Optional[Dict[str, Any]] = None,
        accept_irreversible: bool = False,
        note: str = "",
    ) -> Revision:
        record = self._proposals.get(change_id)
        if record is None:
            raise LedgerError(f"unknown or already resolved change {change_id}")
        if record.status is not ChangeStatus.PROPOSED:
            raise LedgerError(f"change {change_id} is {record.status.value}")

        if override_params:
            merged = {**record.params, **override_params}
            record = ChangeRecord(
                id=record.id,
                task_id=record.task_id,
                kind=record.kind,
                target=record.target,
                reason=record.reason,
                params=merged,
                diff=record.diff,
                impact=record.impact,
                reversible=record.reversible,
                requires_ack=record.requires_ack,
                status=record.status,
                created_at=record.created_at,
            )
            try:
                validate(record, self._state)
            except ChangeRejected as exc:
                raise LedgerError(str(exc)) from exc

        if not record.reversible and not accept_irreversible:
            raise LedgerError(
                "this change has external side effects and requires explicit acknowledgement"
            )

        before = normalise_state(self._state)
        try:
            after = apply_to_state(record, self._state)
        except ChangeRejected as exc:
            raise LedgerError(str(exc)) from exc

        version = self._store.next_version()
        record.status = ChangeStatus.APPLIED
        record.version = version
        record.diff = _build_diff(record, before, after)

        revision = Revision(
            version=version,
            change=record,
            before=before,
            after=after,
            applied_at=time.time(),
            note=note,
        )

        self._state = after
        self._store.save_state(self._state)
        self._store.append_revision(revision)
        self._store.delete_proposal(change_id)
        self._proposals.pop(change_id, None)
        self._revisions.append(revision)

        self._emit("change-applied", {"revision": revision.to_dict(), "state": self._state})
        return revision

    # -- revert / redo ------------------------------------------------------
    def revert(self, version: int) -> List[Revision]:
        """Chain-revert: undo ``version`` and everything applied after it."""
        target = self._revision(version)
        if target.reverted_at is not None:
            raise LedgerError(f"version {version} is already reverted")

        pending = [
            r for r in self._revisions
            if r.version >= version and r.reverted_at is None
        ]
        if not pending:
            raise LedgerError(f"nothing to revert at or after version {version}")

        reverted: List[Revision] = []
        # Restore the snapshot captured *before* the oldest revision in the chain.
        restore_point = min(pending, key=lambda r: r.version).before
        now = time.time()

        # Walk newest -> oldest so per-revision metadata stays coherent.
        for revision in sorted(pending, key=lambda r: r.version, reverse=True):
            revision.reverted_at = now
            revision.note = (revision.note + " | " if revision.note else "") + f"reverted by v{version} rollback"
            self._store.update_revision(revision)
            reverted.append(revision)

        self._state = normalise_state(restore_point)
        self._store.save_state(self._state)
        self._emit(
            "change-reverted",
            {
                "versions": [r.version for r in sorted(reverted, key=lambda r: r.version)],
                "restored_state": self._state,
            },
        )
        return sorted(reverted, key=lambda r: r.version)

    def redo(self) -> Optional[Revision]:
        """Re-apply the oldest reverted revision. One step per call."""
        candidates = [r for r in self._revisions if r.reverted_at is not None]
        if not candidates:
            return None
        candidate = min(candidates, key=lambda r: r.version)

        before = normalise_state(self._state)
        try:
            after = apply_to_state(candidate.change, self._state)
        except ChangeRejected as exc:
            raise LedgerError(str(exc)) from exc

        candidate.reverted_at = None
        candidate.note = (candidate.note + " | " if candidate.note else "") + "redone"
        # Record the state this redo was applied to, so the audit trail explains
        # what the revision changed the second time around.
        candidate.before = before
        candidate.after = after
        self._state = after
        self._store.save_state(self._state)
        self._store.update_revision(candidate)
        self._emit(
            "change-redone",
            {"revision": candidate.to_dict(), "state": self._state},
        )
        return candidate

    def redo_all(self) -> List[Revision]:
        out: List[Revision] = []
        while True:
            revision = self.redo()
            if revision is None:
                break
            out.append(revision)
        return out

    # -- history ------------------------------------------------------------
    def list_history(self) -> List[Revision]:
        return sorted(self._revisions, key=lambda r: r.version)

    def _revision(self, version: int) -> Revision:
        for revision in self._revisions:
            if revision.version == version:
                return revision
        raise LedgerError(f"unknown version {version}")

    def active_revisions(self) -> List[Revision]:
        return [r for r in self._revisions if r.reverted_at is None]

    def close(self) -> None:
        self._store.close()


# -- diff helpers -----------------------------------------------------------
def _build_diff(record: ChangeRecord, before: Dict[str, Any], after: Dict[str, Any]):
    hunks: List[DiffHunk] = []
    kind = record.kind

    if kind is ChangeKind.REGISTER_EXECUTOR:
        hunks.append(
            DiffHunk(
                path="executors",
                before=before.get("executors", []),
                after=after.get("executors", []),
            )
        )
    elif kind is ChangeKind.UNREGISTER_EXECUTOR:
        hunks.append(
            DiffHunk(
                path="executors",
                before=before.get("executors", []),
                after=after.get("executors", []),
            )
        )
    elif kind is ChangeKind.REGISTER_MODEL_PROVIDER:
        pid = str(record.params.get("id") or record.params.get("label", "")).strip().lower().replace(" ", "-")
        before_p = next((p for p in before.get("model_providers", []) if p.get("id") == pid), None)
        after_p = next((p for p in after.get("model_providers", []) if p.get("id") == pid), None)
        hunks.append(DiffHunk(path=f"model_providers[{pid}]", before=before_p, after=after_p))
    elif kind is ChangeKind.UPDATE_SYSTEM_PROMPT:
        sid = str(record.params.get("segment_id", "core"))
        before_s = next((s for s in before.get("system_prompt_segments", []) if s["id"] == sid), None)
        after_s = next((s for s in after.get("system_prompt_segments", []) if s["id"] == sid), None)
        hunks.append(DiffHunk(path=f"system_prompt_segments[{sid}]", before=before_s, after=after_s))
    elif kind is ChangeKind.UPDATE_UI_PANEL:
        pid = str(record.params.get("panel_id"))
        before_p = next((p for p in before.get("ui_panels", []) if p["id"] == pid), None)
        after_p = next((p for p in after.get("ui_panels", []) if p["id"] == pid), None)
        hunks.append(DiffHunk(path=f"ui_panels[{pid}]", before=before_p, after=after_p))
    elif kind is ChangeKind.UPDATE_SKILL_REGISTRY:
        name = str(record.params.get("name", ""))
        before_s = next((s for s in before.get("skills", []) if s.get("name") == name), None)
        after_s = next((s for s in after.get("skills", []) if s.get("name") == name), None)
        hunks.append(DiffHunk(path=f"skills[{name}]", before=before_s, after=after_s))
    elif kind in (ChangeKind.REGISTER_MCP_TOOL, ChangeKind.UNREGISTER_MCP_TOOL):
        name = str(record.params.get("name", ""))
        before_t = next((t for t in before.get("mcp_tools", []) if t.get("name") == name), None)
        after_t = next((t for t in after.get("mcp_tools", []) if t.get("name") == name), None)
        hunks.append(DiffHunk(path=f"mcp_tools[{name}]", before=before_t, after=after_t))
        if kind is ChangeKind.REGISTER_MCP_TOOL and record.params.get("code"):
            hunks.append(
                DiffHunk(
                    path=f"mcp/{name}/tool.py",
                    before=None,
                    after=str(record.params.get("code", "")),
                )
            )

    return hunks


def _default_impact(record: ChangeRecord) -> List[str]:
    mapping = {
        ChangeKind.REGISTER_EXECUTOR: ["tool catalogue", "system prompt capabilities"],
        ChangeKind.UNREGISTER_EXECUTOR: ["tool catalogue", "running tasks may lose a tool"],
        ChangeKind.REGISTER_MODEL_PROVIDER: ["model selector options"],
        ChangeKind.UPDATE_SYSTEM_PROMPT: ["all future model calls"],
        ChangeKind.UPDATE_UI_PANEL: ["GUI layout only"],
        ChangeKind.UPDATE_SKILL_REGISTRY: ["available skills", "tool catalogue"],
        ChangeKind.REGISTER_MCP_TOOL: ["tool catalogue", "local mcp/ directory"],
        ChangeKind.UNREGISTER_MCP_TOOL: ["tool catalogue", "local mcp/ directory"],
    }
    return mapping.get(record.kind, ["runtime"])


__all__ = ["ChangeLedger", "LedgerError"]