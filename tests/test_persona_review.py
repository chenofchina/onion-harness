"""Persona, automatic safety review, and transcript encryption.

The assertions worth reading twice are the ones about ordering and refusal: a
persona must not be able to move the mode contract, and an unreachable reviewer
must refuse rather than wave the command through.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from onion_harness.api.secrets import MemorySecretStore, cipher_from_store
from onion_harness.api.store import AppStore, DEFAULT_SETTINGS
from onion_harness.core.persona import (
    BUILTIN_PERSONAS,
    DEFAULT_PERSONA_ID,
    PersonaLibrary,
    PersonaSpec,
    compose_persona_overlay,
    persona_for,
)
from onion_harness.harness.adapters import SessionFactory
from onion_harness.harness.review import (
    DECISION_PASS,
    DECISION_REJECT,
    ReviewConfig,
    ReviewGate,
    ReviewResult,
    looks_destructive,
    parse_answer,
)
from onion_harness.harness.runtime import ModelConfig, build_model_config


# ------------------------------------------------------------------ persona
class PersonaSpecTest(unittest.TestCase):
    def test_builtins_are_well_formed(self):
        self.assertGreaterEqual(len(BUILTIN_PERSONAS), 3)
        ids = [spec.id for spec in BUILTIN_PERSONAS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(DEFAULT_PERSONA_ID, ids)
        for spec in BUILTIN_PERSONAS:
            self.assertTrue(0.0 <= spec.temperature <= 2.0)
            self.assertTrue(0.0 <= spec.top_p <= 1.0)

    def test_sampling_is_clamped_at_construction(self):
        spec = PersonaSpec(id="x", label="X", overlay="y", temperature=9, top_p=9)
        self.assertEqual(spec.temperature, 2.0)
        self.assertEqual(spec.top_p, 1.0)

    def test_unknown_id_falls_back_to_default(self):
        self.assertEqual(persona_for("nope").id, DEFAULT_PERSONA_ID)
        self.assertEqual(persona_for(None).id, DEFAULT_PERSONA_ID)
        self.assertEqual(persona_for("OPERATOR").id, "operator")

    def test_overlay_is_stacked_after_the_base(self):
        spec = persona_for("operator")
        composed = compose_persona_overlay(spec, "CORE RULES")
        self.assertTrue(composed.startswith("CORE RULES"))
        self.assertIn("Persona: Operator", composed)
        self.assertLess(composed.index("CORE RULES"), composed.index("Persona:"))

    def test_default_persona_adds_nothing(self):
        self.assertEqual(compose_persona_overlay(persona_for("default"), "CORE"), "CORE")

    def test_to_dict_shape(self):
        data = persona_for("auditor").to_dict()
        self.assertEqual(data["id"], "auditor")
        self.assertEqual(set(data["sampling"]), {"temperature", "top_p"})


class PersonaLibraryTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-persona-"))

    def _write(self, payload, name="custom.json"):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_builtins_load_without_a_directory(self):
        library = PersonaLibrary.load(None)
        self.assertTrue(any(spec.id == DEFAULT_PERSONA_ID for spec in library.personas))
        self.assertEqual(library.errors, [])

    def test_custom_file_is_loaded(self):
        self._write(
            {
                "id": "terse",
                "label": "Terse",
                "overlay": "Answer in as few words as possible.",
                "sampling": {"temperature": 0.4, "top_p": 0.8},
            }
        )
        library = PersonaLibrary.load(self.dir)
        spec = library.get("terse")
        self.assertEqual(spec.label, "Terse")
        self.assertFalse(spec.builtin)
        self.assertAlmostEqual(spec.temperature, 0.4)

    def test_missing_sampling_is_rejected_not_defaulted(self):
        path = self._write({"id": "bad", "label": "Bad", "overlay": "hi"})
        library = PersonaLibrary.load(self.dir)
        self.assertIsNone(
            next((s for s in library.personas if s.id == "bad"), None),
            "a persona without sampling must not load",
        )
        self.assertTrue(any("sampling" in error for error in library.errors))
        self.assertTrue(path.exists())

    def test_partial_sampling_is_rejected(self):
        self._write(
            {"id": "half", "label": "Half", "overlay": "x", "sampling": {"temperature": 0.5}}
        )
        library = PersonaLibrary.load(self.dir)
        self.assertIsNone(next((s for s in library.personas if s.id == "half"), None))
        self.assertTrue(any("top_p" in error for error in library.errors))

    def test_out_of_range_sampling_rejected(self):
        self._write(
            {"id": "hot", "label": "Hot", "overlay": "x", "sampling": {"temperature": 5, "top_p": 1}}
        )
        library = PersonaLibrary.load(self.dir)
        self.assertIsNone(next((s for s in library.personas if s.id == "hot"), None))

    def test_id_colliding_with_a_builtin_is_rejected(self):
        self._write(
            {
                "id": "operator",
                "label": "Fake",
                "overlay": "x",
                "sampling": {"temperature": 1, "top_p": 1},
            }
        )
        library = PersonaLibrary.load(self.dir)
        self.assertEqual(library.get("operator").label, "Operator")
        self.assertTrue(any("collides" in error for error in library.errors))

    def test_broken_json_is_reported(self):
        (self.dir / "broken.json").write_text("{not json", encoding="utf-8")
        library = PersonaLibrary.load(self.dir)
        self.assertTrue(library.errors)
        # The library still works.
        self.assertEqual(library.get("default").id, DEFAULT_PERSONA_ID)


class PersonaSamplingTest(unittest.TestCase):
    def test_persona_supplies_sampling_when_nothing_is_set(self):
        model = build_model_config({"provider_id": "deepseek", "persona_id": "operator"})
        self.assertEqual(model.persona_id, "operator")
        self.assertAlmostEqual(model.temperature, persona_for("operator").temperature)
        self.assertAlmostEqual(model.top_p, persona_for("operator").top_p)

    def test_explicit_value_beats_the_persona(self):
        model = build_model_config(
            {"provider_id": "deepseek", "persona_id": "operator", "temperature": 1.4, "top_p": 0.4}
        )
        self.assertAlmostEqual(model.temperature, 1.4)
        self.assertAlmostEqual(model.top_p, 0.4)

    def test_top_p_is_clamped_to_its_own_range(self):
        model = build_model_config({"provider_id": "deepseek", "top_p": 9})
        self.assertEqual(model.top_p, 1.0)
        model = build_model_config({"provider_id": "deepseek", "top_p": -3})
        self.assertEqual(model.top_p, 0.0)

    def test_garbage_falls_back_to_the_persona(self):
        model = build_model_config({"provider_id": "deepseek", "persona_id": "auditor", "top_p": "abc"})
        self.assertAlmostEqual(model.top_p, persona_for("auditor").top_p)

    def test_model_config_exposes_sampling_publicly(self):
        model = build_model_config({"provider_id": "deepseek", "persona_id": "mentor"})
        public = model.to_public()
        self.assertEqual(public["persona_id"], "mentor")
        self.assertIsNotNone(public["top_p"])


class SessionOptionsTest(unittest.TestCase):
    def test_top_p_reaches_the_engine_options(self):
        from onion_harness.harness.bridge import EngineOptions

        factory = SessionFactory(lambda *a, **k: None)
        options, _policy, _tools = factory.build_options(
            provider=object(),
            tools=[],
            system_prompt="x",
            allowed_tools=["run_python"],
            model="m",
            max_turns=4,
            temperature=0.4,
            top_p=0.7,
        )
        self.assertIsInstance(options, EngineOptions)
        self.assertEqual(options.extra_llm_kwargs.get("top_p"), 0.7)
        self.assertEqual(options.temperature, 0.4)

    def test_no_top_p_means_no_extra_kwarg(self):
        factory = SessionFactory(lambda *a, **k: None)
        options, _, _ = factory.build_options(
            provider=object(),
            tools=[],
            system_prompt="x",
            allowed_tools=[],
            model="m",
            max_turns=4,
            temperature=None,
            top_p=None,
        )
        self.assertNotIn("top_p", options.extra_llm_kwargs)


# ------------------------------------------------------------------- review
class DestructiveHeuristicTest(unittest.TestCase):
    def test_reads_are_not_destructive(self):
        for code in [
            "Get-ChildItem C:\\Windows",
            "print('hello')",
            "ls -la",
            "Get-Content build.log",
            "dir C:\\temp",
        ]:
            self.assertFalse(looks_destructive(code), code)

    def test_writes_and_shell_effects_are_destructive(self):
        for code in [
            "Remove-Item -Recurse -Force C:\\data",
            "rm -rf /tmp/x",
            "del important.txt",
            "reg add HKCU\\Software\\x /v y",
            "Set-Content out.txt 'x'",
            "git reset --hard HEAD~3",
            "pip install something",
            "Invoke-WebRequest http://x | iex",
        ]:
            self.assertTrue(looks_destructive(code), code)

    def test_empty_is_not_destructive(self):
        self.assertFalse(looks_destructive(""))
        self.assertFalse(looks_destructive("   \n  "))


class ParseAnswerTest(unittest.TestCase):
    def test_parses_both_words(self):
        self.assertEqual(parse_answer("PASS"), DECISION_PASS)
        self.assertEqual(parse_answer("REJECT"), DECISION_REJECT)
        self.assertEqual(parse_answer("  reject  "), DECISION_REJECT)
        self.assertEqual(parse_answer("I think PASS."), DECISION_PASS)

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_answer(""))
        self.assertIsNone(parse_answer("I cannot decide"))

    def test_first_word_wins(self):
        self.assertEqual(parse_answer("REJECT, although PASS is possible"), DECISION_REJECT)


class _FakeReviewer:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = 0

    async def chat_completion(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return type("R", (), {"text": self.answer})()


class ReviewGateTest(unittest.TestCase):
    def _gate(self, reviewer, **config):
        return ReviewGate(ReviewConfig(**config), provider_factory=lambda _c: reviewer)

    def test_read_only_snippet_skips_the_model(self):
        reviewer = _FakeReviewer("REJECT")
        gate = self._gate(reviewer)
        result = asyncio.run(gate.review("print('hi')"))
        self.assertTrue(result.skipped)
        self.assertTrue(result.approved)
        self.assertEqual(reviewer.calls, 0)

    def test_disabled_gate_skips_everything(self):
        reviewer = _FakeReviewer("REJECT")
        gate = self._gate(reviewer, enabled=False)
        result = asyncio.run(gate.review("rm -rf /"))
        self.assertTrue(result.skipped)
        self.assertEqual(reviewer.calls, 0)

    def test_pass_allows(self):
        gate = self._gate(_FakeReviewer("PASS"))
        result = asyncio.run(gate.review("Set-Content notes.txt 'ok'"))
        self.assertTrue(result.approved)
        self.assertFalse(result.skipped)

    def test_reject_blocks_with_a_code_warning(self):
        gate = self._gate(_FakeReviewer("REJECT"))
        result = asyncio.run(gate.review("Remove-Item -Recurse C:\\"))
        self.assertFalse(result.approved)
        message = result.blocked_message()
        self.assertIn("destructive", message)
        self.assertNotIn("unavailable", message)

    def test_unreachable_reviewer_fails_closed(self):
        gate = self._gate(_FakeReviewer(error=RuntimeError("connection refused")))
        result = asyncio.run(gate.review("rm -rf /tmp/x"))
        self.assertFalse(result.approved)
        self.assertIn("unavailable", result.blocked_message())
        # The message must not blame the code, or users learn to distrust the gate.
        self.assertNotIn("destructive or high-risk", result.blocked_message())

    def test_unreachable_reviewer_can_be_configured_open(self):
        gate = self._gate(_FakeReviewer(error=RuntimeError("boom")), fail_closed=False)
        result = asyncio.run(gate.review("rm -rf /tmp/x"))
        self.assertTrue(result.approved)

    def test_unusable_answer_is_treated_as_unavailable(self):
        gate = self._gate(_FakeReviewer("I am not sure what you mean"))
        result = asyncio.run(gate.review("Remove-Item x"))
        self.assertFalse(result.approved)
        self.assertIn("unavailable", result.reason)

    def test_config_is_clamped(self):
        config = ReviewConfig.from_public({"timeout_seconds": 9999})
        self.assertLessEqual(config.timeout_seconds, 60.0)
        config = ReviewConfig.from_public({"timeout_seconds": 0.1})
        self.assertGreaterEqual(config.timeout_seconds, 2.0)

    def test_config_roundtrip(self):
        original = ReviewConfig(enabled=False, model="m", timeout_seconds=9, fail_closed=False)
        restored = ReviewConfig.from_public(original.to_public())
        self.assertEqual(restored.to_public(), original.to_public())


class ReviewResultTest(unittest.TestCase):
    def test_approved_property(self):
        self.assertTrue(ReviewResult(DECISION_PASS).approved)
        self.assertFalse(ReviewResult(DECISION_REJECT).approved)

    def test_to_dict_is_json_safe(self):
        json.dumps(ReviewResult(DECISION_PASS).to_dict())


# ---------------------------------------------------- transcript encryption
class TranscriptEncryptionTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-tx-"))
        self.cipher = cipher_from_store(MemorySecretStore())
        self.store = AppStore(self.dir, cipher=self.cipher)

    def test_roundtrip_through_the_store(self):
        payload = {"id": "e1", "type": "user-input", "payload": {"text": "hello"}}
        self.store.append_event("t1", payload)
        self.assertEqual(self.store.load_events("t1"), [payload])

    def test_file_contains_no_plaintext(self):
        self.store.append_event("t1", {"type": "user-input", "payload": {"text": "SECRETPROMPT"}})
        path = self.dir / "tasks" / "t1.jsonl"
        raw = path.read_bytes()
        self.assertNotIn(b"SECRETPROMPT", raw)
        self.assertNotIn(b"user-input", raw)
        self.assertTrue(path.read_text(encoding="utf-8").startswith("onion:v1:"))

    def test_appends_stay_readable_in_order(self):
        for index in range(5):
            self.store.append_event("t1", {"id": f"e{index}", "n": index})
        events = self.store.load_events("t1")
        self.assertEqual([e["n"] for e in events], list(range(5)))

    def test_plaintext_file_is_migrated(self):
        path = self.dir / "tasks" / "old.jsonl"
        path.write_text(
            json.dumps({"id": "e1", "payload": {"text": "LEGACY"}}) + "\n", encoding="utf-8"
        )
        rewritten = self.store.migrate_plaintext_events()
        self.assertEqual(rewritten, 1)
        self.assertNotIn(b"LEGACY", path.read_bytes())
        self.assertEqual(len(self.store.load_events("old")), 1)

    def test_migration_is_idempotent(self):
        self.store.append_event("t1", {"id": "e1"})
        self.assertEqual(self.store.migrate_plaintext_events(), 0)

    def test_unreadable_sealed_line_is_skipped_not_fatal(self):
        path = self.dir / "tasks" / "bad.jsonl"
        path.write_text("onion:v1:not-base64!!\n", encoding="utf-8")
        self.assertEqual(self.store.load_events("bad"), [])

    def test_missing_key_yields_no_events(self):
        self.store.append_event("t1", {"id": "e1", "payload": {"text": "x"}})
        locked = AppStore(self.dir, cipher=None)
        # force the "no cipher" path rather than re-deriving from the vault
        locked._cipher = None
        self.assertEqual(locked.load_events("t1"), [])

    def test_settings_stay_plain_json(self):
        # Settings hold no conversation content, only ids and window sizes, so
        # leaving them readable keeps the file inspectable when something is off.
        self.store.save_settings(dict(DEFAULT_SETTINGS))
        raw = (self.dir / "settings.json").read_text(encoding="utf-8")
        self.assertTrue(raw.lstrip().startswith("{"))
        json.loads(raw)

    def test_task_index_is_sealed_and_roundtrips(self):
        tasks = [{"id": "t1", "prompt": "SECRETQUESTION", "status": "completed"}]
        self.store.save_tasks(tasks)
        raw = (self.dir / "tasks.json").read_bytes()
        self.assertNotIn(b"SECRETQUESTION", raw)
        self.assertTrue(raw.startswith(b"onion:v1:"))
        self.assertEqual(self.store.load_tasks(), tasks)

    def test_plaintext_task_index_is_still_readable(self):
        legacy = [{"id": "old", "prompt": "legacy prompt"}]
        (self.dir / "tasks.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(self.store.load_tasks(), legacy)
        # Saving again seals it.
        self.store.save_tasks(legacy)
        self.assertTrue((self.dir / "tasks.json").read_bytes().startswith(b"onion:v1:"))

    def test_missing_key_gives_no_tasks_rather_than_garbage(self):
        self.store.save_tasks([{"id": "t1", "prompt": "x"}])
        locked = AppStore(self.dir, cipher=None)
        locked._cipher = None
        self.assertEqual(locked.load_tasks(), [])


class ServiceSettingsTest(unittest.TestCase):
    """The service must persist persona choice and review config correctly."""

    def setUp(self):
        from onion_harness.api.service import OnionService

        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-svc-")))
        self.service.start()
        self.service.update_settings({"provider_id": "deepseek", "api_key": "sk-test"})

    def tearDown(self):
        self.service.stop()

    def test_bootstrap_exposes_personas_and_review(self):
        boot = self.service.bootstrap()
        self.assertTrue(boot["personas"])
        self.assertIn("active_persona", boot)
        self.assertIn("auto_review", boot)
        self.assertEqual(boot["active_persona"]["id"], "default")

    def test_persona_switch_is_persisted_and_hot(self):
        settings = self.service.update_settings({"active_persona": "operator"})
        self.assertEqual(settings["active_persona"], "operator")
        self.assertEqual(self.service.bootstrap()["active_persona"]["id"], "operator")

    def test_unknown_persona_is_rejected(self):
        from onion_harness.api.service import ApiError

        with self.assertRaises(ApiError):
            self.service.update_settings({"active_persona": "does-not-exist"})

    def test_new_task_picks_up_the_active_persona(self):
        self.service.update_settings({"active_persona": "auditor"})
        record = self.service.create_task({"prompt": "look around"})
        self.assertEqual(record["persona_id"], "auditor")
        self.assertAlmostEqual(record["temperature"], persona_for("auditor").temperature)

    def test_explicit_sampling_survives_and_wins(self):
        self.service.update_settings({"active_persona": "auditor"})
        record = self.service.create_task(
            {"prompt": "x", "model": {"temperature": 1.5, "top_p": 0.5}}
        )
        self.assertAlmostEqual(record["temperature"], 1.5)
        self.assertAlmostEqual(record["top_p"], 0.5)

    def test_review_config_roundtrips_and_hot_applies(self):
        updated = self.service.update_settings(
            {"auto_review": {"enabled": False, "model": "other-model"}}
        )
        self.assertFalse(updated["auto_review"]["enabled"])
        self.assertEqual(updated["auto_review"]["model"], "other-model")
        self.assertFalse(self.service.review_gate.config.enabled)
        self.assertEqual(self.service.review_gate.config.model, "other-model")

    def test_review_timeout_is_clamped(self):
        updated = self.service.update_settings({"auto_review": {"timeout_seconds": 9999}})
        self.assertLessEqual(updated["auto_review"]["timeout_seconds"], 60.0)


class MemoryApiTest(unittest.TestCase):
    """Memory must be editable from the GUI, not only machine-written."""

    def setUp(self):
        from onion_harness.api.service import OnionService

        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-memapi-")))
        self.service.start()

    def tearDown(self):
        self.service.stop()

    def test_add_then_list(self):
        record = self.service.memory_add({"text": "用户偏好使用 PowerShell", "kind": "preference"})
        self.assertEqual(record["kind"], "preference")
        listed = self.service.memory_list()
        self.assertEqual(len(listed), 1)
        self.assertIn("PowerShell", listed[0]["text"])

    def test_add_requires_text(self):
        from onion_harness.api.service import ApiError

        with self.assertRaises(ApiError):
            self.service.memory_add({"text": "   "})

    def test_update_changes_the_note(self):
        record = self.service.memory_add({"text": "first"})
        updated = self.service.memory_update(record["id"], {"text": "second", "kind": "fact"})
        self.assertEqual(updated["text"], "second")
        self.assertEqual(self.service.memory_list()[0]["text"], "second")

    def test_update_of_a_missing_id_is_404(self):
        from onion_harness.api.service import ApiError

        with self.assertRaises(ApiError) as ctx:
            self.service.memory_update("mem_nope", {"text": "x"})
        self.assertEqual(ctx.exception.status, 404)

    def test_update_with_no_fields_is_rejected(self):
        from onion_harness.api.service import ApiError

        record = self.service.memory_add({"text": "x"})
        with self.assertRaises(ApiError):
            self.service.memory_update(record["id"], {})

    def test_edit_survives_a_restart(self):
        from onion_harness.api.service import OnionService

        record = self.service.memory_add({"text": "before restart"})
        self.service.memory_update(record["id"], {"text": "after restart"})
        self.service.stop()

        again = OnionService(data_dir=self.service.data_dir)
        try:
            self.assertEqual(again.memory_list()[0]["text"], "after restart")
        finally:
            again.stop()
        # Re-open so tearDown has something to stop.
        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-memapi2-")))
        self.service.start()


if __name__ == "__main__":
    unittest.main()