"""Change-ledger tests - staging, versioning, chain rollback, redo, persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from onion_harness.ledger import (
    ChangeKind,
    ChangeLedger,
    ChangeRecord,
    LedgerError,
    LedgerStore,
)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-ledger-"))
        self.db = self.dir / "ledger.sqlite3"
        self.events = []
        self.ledger = ChangeLedger(LedgerStore(self.db), emit=lambda k, p: self.events.append(k))

    def tearDown(self):
        self.ledger.close()

    def prompt_change(self, content="new prompt", **extra):
        return ChangeRecord(
            kind=ChangeKind.UPDATE_SYSTEM_PROMPT,
            target="core",
            reason="test",
            params={"segment_id": "core", "content": content, **extra},
        )


class ProposeTest(LedgerTestCase):
    def test_propose_does_not_apply(self):
        before = self.ledger.state["system_prompt_segments"][0]["content"]
        self.ledger.propose(self.prompt_change())
        after = self.ledger.state["system_prompt_segments"][0]["content"]
        self.assertEqual(before, after)
        self.assertEqual(len(self.ledger.list_proposals()), 1)

    def test_propose_builds_structured_diff(self):
        change_id = self.ledger.propose(self.prompt_change("brand new instructions"))
        record = self.ledger.get_proposal(change_id)
        self.assertTrue(record.diff)
        self.assertEqual(record.diff[0].path, "system_prompt_segments[core]")
        self.assertEqual(record.diff[0].after["content"], "brand new instructions")

    def test_proposal_emits_event(self):
        self.ledger.propose(self.prompt_change())
        self.assertIn("change-proposal", self.events)

    def test_invalid_change_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.propose(
                ChangeRecord(kind=ChangeKind.UPDATE_UI_PANEL, target="nope", params={"panel_id": "nope"})
            )

    def test_ui_panel_only_accepts_predefined_positions(self):
        with self.assertRaises(LedgerError):
            self.ledger.propose(
                ChangeRecord(
                    kind=ChangeKind.UPDATE_UI_PANEL,
                    target="tool-monitor",
                    params={"panel_id": "tool-monitor", "position": "<script>alert(1)</script>"},
                )
            )

    def test_reject_removes_proposal(self):
        change_id = self.ledger.propose(self.prompt_change())
        self.ledger.discard(change_id, "no thanks")
        self.assertEqual(self.ledger.list_proposals(), [])
        self.assertIn("change-rejected", self.events)

    def test_duplicate_ids_rejected(self):
        record = ChangeRecord(id="fixed", kind=ChangeKind.UPDATE_SYSTEM_PROMPT, params={"content": "a"})
        self.ledger.propose(record)
        with self.assertRaises(LedgerError):
            self.ledger.propose(record)


class ApplyTest(LedgerTestCase):
    def test_apply_bumps_version_and_persists(self):
        change_id = self.ledger.propose(self.prompt_change("v1 prompt"))
        revision = self.ledger.apply(change_id)
        self.assertEqual(revision.version, 1)
        self.assertEqual(self.ledger.state["system_prompt_segments"][0]["content"], "v1 prompt")
        self.assertEqual(len(self.ledger.list_history()), 1)

    def test_apply_unknown_id(self):
        with self.assertRaises(LedgerError):
            self.ledger.apply("does-not-exist")

    def test_apply_twice_fails(self):
        change_id = self.ledger.propose(self.prompt_change())
        self.ledger.apply(change_id)
        with self.assertRaises(LedgerError):
            self.ledger.apply(change_id)

    def test_override_params_validated(self):
        change_id = self.ledger.propose(self.prompt_change("original"))
        revision = self.ledger.apply(change_id, override_params={"content": "edited by user"})
        self.assertEqual(
            self.ledger.state["system_prompt_segments"][0]["content"], "edited by user"
        )
        self.assertEqual(revision.change.params["content"], "edited by user")

    def test_irreversible_requires_acknowledgement(self):
        change_id = self.ledger.propose(
            ChangeRecord(
                kind=ChangeKind.UPDATE_SKILL_REGISTRY,
                target="pdf",
                params={"action": "mount", "name": "pdf", "path": "skills/pdf"},
            )
        )
        record = self.ledger.get_proposal(change_id)
        self.assertFalse(record.reversible)
        self.assertTrue(record.requires_ack)
        with self.assertRaises(LedgerError):
            self.ledger.apply(change_id)
        revision = self.ledger.apply(change_id, accept_irreversible=True)
        self.assertEqual(revision.version, 1)


class ChainRollbackTest(LedgerTestCase):
    def _three_changes(self):
        ids = []
        ids.append(self.ledger.apply(self.ledger.propose(self.prompt_change("one"))).version)
        ids.append(
            self.ledger.apply(
                self.ledger.propose(
                    ChangeRecord(
                        kind=ChangeKind.REGISTER_MODEL_PROVIDER,
                        target="Local",
                        params={"label": "Local", "base_url": "http://localhost:11434/v1"},
                    )
                )
            ).version
        )
        ids.append(
            self.ledger.apply(
                self.ledger.propose(
                    ChangeRecord(
                        kind=ChangeKind.UPDATE_UI_PANEL,
                        target="tool-monitor",
                        params={"panel_id": "tool-monitor", "visible": False},
                    )
                )
            ).version
        )
        return ids

    def test_revert_middle_rolls_back_later_versions_too(self):
        v1, v2, v3 = self._three_changes()
        reverted = self.ledger.revert(v2)
        versions = [r.version for r in reverted]
        self.assertEqual(versions, [v2, v3])

        state = self.ledger.state
        # v1's prompt change survives; v2 and v3 are undone together.
        self.assertEqual(state["model_providers"], [])
        self.assertEqual(state["system_prompt_segments"][0]["content"], "one")
        self.assertTrue(
            next(p for p in state["ui_panels"] if p["id"] == "tool-monitor")["visible"]
        )

    def test_revert_preserves_history_entries(self):
        _, v2, v3 = self._three_changes()
        self.ledger.revert(v2)
        history = self.ledger.list_history()
        self.assertEqual(len(history), 3)
        reverted = [r.version for r in history if r.reverted_at is not None]
        self.assertEqual(reverted, [v2, v3])
        self.assertEqual([r.version for r in self.ledger.active_revisions()], [1])

    def test_revert_oldest_restores_initial_state(self):
        self._three_changes()
        self.ledger.revert(1)
        self.assertEqual(self.ledger.state["model_providers"], [])
        self.assertTrue(self.ledger.state["ui_panels"])

    def test_revert_already_reverted_fails(self):
        _, v2, _ = self._three_changes()
        self.ledger.revert(v2)
        with self.assertRaises(LedgerError):
            self.ledger.revert(v2)

    def test_redo_reapplies_one_step_at_a_time(self):
        _, v2, v3 = self._three_changes()
        self.ledger.revert(v2)
        first = self.ledger.redo()
        self.assertEqual(first.version, v2)
        self.assertEqual(len(self.ledger.state["model_providers"]), 1)
        second = self.ledger.redo()
        self.assertEqual(second.version, v3)
        self.assertIsNone(self.ledger.redo())

    def test_redo_all(self):
        _, v2, v3 = self._three_changes()
        self.ledger.revert(v2)
        redone = self.ledger.redo_all()
        self.assertEqual([r.version for r in redone], [v2, v3])
        self.assertEqual(self.ledger.active_revisions()[1].version, v2)


class PersistenceTest(unittest.TestCase):
    def test_state_and_history_survive_restart(self):
        directory = Path(tempfile.mkdtemp(prefix="onion-persist-"))
        db = directory / "ledger.sqlite3"

        first = ChangeLedger(LedgerStore(db))
        change_id = first.propose(
            ChangeRecord(
                kind=ChangeKind.UPDATE_SYSTEM_PROMPT,
                target="core",
                params={"segment_id": "core", "content": "persisted prompt"},
            )
        )
        first.apply(change_id)
        first.close()

        second = ChangeLedger(LedgerStore(db))
        self.assertEqual(
            second.state["system_prompt_segments"][0]["content"], "persisted prompt"
        )
        self.assertEqual(len(second.list_history()), 1)
        second.close()

    def test_pending_proposals_survive_restart(self):
        directory = Path(tempfile.mkdtemp(prefix="onion-pending-"))
        db = directory / "ledger.sqlite3"

        first = ChangeLedger(LedgerStore(db))
        change_id = first.propose(
            ChangeRecord(kind=ChangeKind.UPDATE_SYSTEM_PROMPT, target="core", params={"content": "pending"})
        )
        first.close()

        second = ChangeLedger(LedgerStore(db))
        pending = second.list_proposals()
        self.assertEqual([p.id for p in pending], [change_id])
        second.close()

    def test_ledger_is_independent_of_sessions(self):
        """No session object is required to read or apply history."""
        directory = Path(tempfile.mkdtemp(prefix="onion-session-"))
        ledger = ChangeLedger(LedgerStore(directory / "l.db"))
        change_id = ledger.propose(
            ChangeRecord(kind=ChangeKind.UPDATE_SYSTEM_PROMPT, params={"content": "x"})
        )
        ledger.apply(change_id)
        ledger.close()

        reloaded = ChangeLedger(LedgerStore(directory / "l.db"))
        version = reloaded.list_history()[0].version
        self.assertEqual(version, 1)
        reloaded.revert(1)
        self.assertIsNotNone(reloaded.list_history()[0].reverted_at)
        reloaded.close()


class LedgerIsTheOnlyGateTest(unittest.TestCase):
    def test_apply_functions_are_not_synchronous_side_effects(self):
        """propose must never mutate live state, in any change kind."""
        directory = Path(tempfile.mkdtemp(prefix="onion-gate-"))
        ledger = ChangeLedger(LedgerStore(directory / "l.db"))
        snapshot = repr(ledger.state)
        for make in [
            lambda: ChangeRecord(kind=ChangeKind.UPDATE_SYSTEM_PROMPT, params={"content": "x"}),
            lambda: ChangeRecord(kind=ChangeKind.REGISTER_EXECUTOR, params={"name": "node"}),
            lambda: ChangeRecord(
                kind=ChangeKind.REGISTER_MODEL_PROVIDER,
                params={"label": "X", "base_url": "http://x/v1"},
            ),
            lambda: ChangeRecord(
                kind=ChangeKind.UPDATE_UI_PANEL,
                params={"panel_id": "event-stream", "visible": False},
            ),
            lambda: ChangeRecord(
                kind=ChangeKind.UPDATE_SKILL_REGISTRY,
                params={"action": "mount", "name": "docx"},
            ),
        ]:
            ledger.propose(make())
        self.assertEqual(repr(ledger.state), snapshot)
        ledger.close()


if __name__ == "__main__":
    unittest.main()