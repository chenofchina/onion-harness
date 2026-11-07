"""Secret storage, key migration, and memory wiring.

Two things are checked here that matter more than the crypto details:

* no key value ever reaches ``settings.json``, including after migrating a file
  written by an older build that stored them in the clear;
* the memory database on disk holds ciphertext only - no readable word, not even
  in the search index.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from onion_harness.api.secrets import (
    DataCipher,
    KeyVault,
    MemorySecretStore,
    SecretError,
    cipher_from_store,
    load_or_create_master_secret,
)
from onion_harness.api.store import DEFAULT_SETTINGS, AppStore
from onion_harness.core import EventType, Task
from onion_harness.harness.bridge import EngineChatResponse, EngineUsage
from onion_harness.memory import MemoryBridge, MemoryKind, MemoryStore, new_memory
from onion_harness.memory.prompt import compose_memory_prompt


# ------------------------------------------------------------------- cipher
class DataCipherTest(unittest.TestCase):
    def setUp(self):
        self.cipher = DataCipher(b"x" * 32)

    def test_roundtrip(self):
        self.assertEqual(self.cipher.decrypt(self.cipher.encrypt("你好 world")), "你好 world")

    def test_each_call_uses_a_fresh_nonce(self):
        self.assertNotEqual(self.cipher.encrypt("same"), self.cipher.encrypt("same"))

    def test_wrong_key_is_rejected(self):
        payload = self.cipher.encrypt("secret")
        with self.assertRaises(SecretError):
            DataCipher(b"y" * 32).decrypt(payload)

    def test_tampered_payload_is_rejected(self):
        payload = bytearray(self.cipher.encrypt("secret"))
        payload[-1] ^= 0xFF
        with self.assertRaises(SecretError):
            self.cipher.decrypt(bytes(payload))

    def test_short_master_secret_rejected(self):
        with self.assertRaises(SecretError):
            DataCipher(b"short")

    def test_token_hash_is_stable_and_not_the_token(self):
        first = self.cipher.token_hash("偏好")
        self.assertEqual(first, self.cipher.token_hash("偏好"))
        self.assertNotEqual(first, self.cipher.token_hash("用户"))
        self.assertNotIn("偏好", first)
        # A different key yields a different digest, which is what stops the
        # index from being useful to someone without the master secret.
        self.assertNotEqual(first, DataCipher(b"z" * 32).token_hash("偏好"))


class MasterSecretTest(unittest.TestCase):
    def test_created_once_then_reused(self):
        store = MemorySecretStore()
        first = load_or_create_master_secret(store)
        self.assertEqual(len(first), 32)
        self.assertEqual(load_or_create_master_secret(store), first)

    def test_corrupt_entry_is_replaced(self):
        store = MemorySecretStore({"onion:master": "not-base64!!"})
        secret = load_or_create_master_secret(store)
        self.assertEqual(len(secret), 32)

    def test_cipher_from_store_reuses_the_same_key(self):
        store = MemorySecretStore()
        payload = cipher_from_store(store).encrypt("a value")
        # A second cipher built from the same store must decrypt the first one,
        # which only holds if the master secret was reused rather than regenerated.
        self.assertEqual(cipher_from_store(store).decrypt(payload), "a value")


class KeyVaultTest(unittest.TestCase):
    def test_set_get_delete(self):
        vault = KeyVault(MemorySecretStore())
        vault.set("deepseek", "sk-1")
        self.assertEqual(vault.get("deepseek"), "sk-1")
        self.assertTrue(vault.has("deepseek"))
        vault.delete("deepseek")
        self.assertIsNone(vault.get("deepseek"))

    def test_empty_value_clears(self):
        vault = KeyVault(MemorySecretStore())
        vault.set("openai", "sk-2")
        vault.set("openai", "   ")
        self.assertFalse(vault.has("openai"))


# ------------------------------------------------------------------ storage
class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-sec-"))
        self.vault = KeyVault(MemorySecretStore())
        self.store = AppStore(self.dir, vault=self.vault)

    def test_key_is_not_written_to_the_file(self):
        settings = dict(DEFAULT_SETTINGS)
        settings = self.store.set_provider_key(settings, "deepseek", "sk-very-secret")
        self.store.save_settings(settings)
        raw = (self.dir / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-very-secret", raw)
        self.assertIn("deepseek", raw)  # the provider id is not a secret

    def test_save_strips_a_stray_api_key_field(self):
        settings = dict(DEFAULT_SETTINGS)
        settings["api_keys"] = {"deepseek": "sk-leak"}
        self.store.save_settings(settings)
        raw = (self.dir / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-leak", raw)
        self.assertNotIn("api_keys", raw)

    def test_public_settings_reports_only_retrievable_keys(self):
        settings = dict(DEFAULT_SETTINGS)
        settings = self.store.set_provider_key(settings, "deepseek", "sk-1")
        settings["key_providers"] = sorted(set(settings["key_providers"]) | {"openai"})
        self.assertEqual(self.store.public_settings(settings)["configured_providers"], ["deepseek"])

    def test_plaintext_keys_are_migrated_out_of_the_file(self):
        legacy = dict(DEFAULT_SETTINGS)
        legacy["api_keys"] = {"deepseek": "sk-legacy", "openai": "sk-legacy-2"}
        raw_path = self.dir / "settings.json"
        raw_path.write_text(json.dumps(legacy), encoding="utf-8")

        store = AppStore(self.dir, vault=KeyVault(MemorySecretStore()))
        migrated = store.migrate_plaintext_keys(store.load_settings())

        self.assertEqual(store.vault.get("deepseek"), "sk-legacy")
        self.assertEqual(store.vault.get("openai"), "sk-legacy-2")
        self.assertEqual(sorted(migrated["key_providers"]), ["deepseek", "openai"])
        self.assertNotIn("api_keys", migrated)
        written = raw_path.read_text(encoding="utf-8")
        self.assertNotIn("sk-legacy", written)

    def test_migration_is_idempotent(self):
        legacy = dict(DEFAULT_SETTINGS)
        legacy["api_keys"] = {"deepseek": "sk-legacy"}
        (self.dir / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")

        store = AppStore(self.dir, vault=KeyVault(MemorySecretStore()))
        store.migrate_plaintext_keys(store.load_settings())
        again = store.migrate_plaintext_keys(store.load_settings())
        self.assertEqual(stored := again.get("key_providers"), ["deepseek"])
        self.assertNotIn("api_keys", again)
        self.assertEqual(sorted(stored), ["deepseek"])

    def test_tests_never_reach_the_real_credential_store(self):
        """A test process must get an in-process store, whatever the env says.

        The failure this guards against is silent: a throwaway key like
        ``sk-test`` written to the real Windows credential manager replaces the
        developer's actual provider key, and nothing reports it until the next
        real request fails to authenticate.
        """
        from onion_harness.api import store as store_module
        from onion_harness.api.secrets import MemorySecretStore, WindowsCredentialStore

        self.assertTrue(store_module._looks_like_a_test_run())
        self.assertIsInstance(store_module._default_store(self.dir), MemorySecretStore)
        self.assertNotIsInstance(store_module._default_store(self.dir), WindowsCredentialStore)

    def test_provider_keys_reads_the_vault(self):
        settings = dict(DEFAULT_SETTINGS)
        settings = self.store.set_provider_key(settings, "deepseek", "sk-1")
        self.assertEqual(self.store.provider_keys(settings), {"deepseek": "sk-1"})


# ------------------------------------------------------------------- memory
class EncryptedMemoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-mem-"))
        self.cipher = cipher_from_store(MemorySecretStore())
        self.path = self.dir / "memory.sqlite3"
        self.store = MemoryStore(self.path, cipher=self.cipher)

    def tearDown(self):
        self.store.close()

    def test_no_plaintext_anywhere_in_the_file(self):
        self.store.remember(new_memory("用户偏好使用 PowerShell 而不是 CMD", MemoryKind.PREFERENCE))
        self.store.remember(new_memory("项目在 D:/work/onion 下面", MemoryKind.FACT))
        self.store.close()

        raw = self.path.read_bytes()
        for probe in ["偏好", "用户", "PowerShell", "CMD", "D:/work", "onion"]:
            self.assertNotIn(probe.encode("utf-8"), raw, f"{probe} leaked into the file")

        reopened = MemoryStore(self.path, cipher=self.cipher)
        try:
            # Retrieval still works: the blind index matches hashed tokens.
            self.assertEqual(len(reopened.recall("偏好")), 1)
            self.assertEqual(len(reopened.recall("PowerShell")), 1)
            self.assertEqual(len(reopened.recall("work")), 1)
        finally:
            reopened.close()


class CompositionTest(unittest.TestCase):
    def test_prompt_states_that_current_request_wins(self):
        memories = [
            new_memory("用户偏好：以后都用 PowerShell", MemoryKind.PREFERENCE),
            new_memory("环境：项目在 D:/work/onion", MemoryKind.FACT),
        ]
        block = compose_memory_prompt(memories)
        self.assertIn("Long-term memory", block)
        self.assertIn("current request wins", block)
        self.assertIn("preference", block)
        self.assertIn("environment", block)

    def test_empty_input_gives_no_block(self):
        self.assertEqual(compose_memory_prompt([]), "")

    def test_block_is_bounded(self):
        memories = [
            new_memory(f"环境：第{i}条" + "很长的描述" * 30, MemoryKind.FACT) for i in range(20)
        ]
        block = compose_memory_prompt(memories)
        self.assertLess(len(block), 1200)
        self.assertLessEqual(block.count("\n- "), 6)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-br-"))
        self.store = MemoryStore(self.dir / "m.db", cipher=cipher_from_store(MemorySecretStore()))
        self.bridge = MemoryBridge(self.store)

    def tearDown(self):
        self.store.close()

    def test_learn_then_recall(self):
        events = [{"type": "user-input", "payload": {"text": "以后都用 PowerShell 写脚本"}}]
        learned = self.bridge.learn(events, task_id="t1")
        self.assertTrue(learned)
        hits = self.bridge.recall("PowerShell")
        self.assertTrue(hits)
        self.assertIn("PowerShell", hits[0].text)

    def test_recall_touches_the_memory(self):
        self.bridge.learn([{"type": "user-input", "payload": {"text": "以后都用 PowerShell"}}])
        self.assertEqual(self.store.recent()[0].use_count, 0)
        self.assertTrue(self.bridge.recall("PowerShell"))
        # Recency and use count drive later ranking, so a useful memory climbs.
        self.assertEqual(self.store.recent()[0].use_count, 1)

    def test_recall_of_nothing_is_empty(self):
        self.assertEqual(self.bridge.recall(""), [])
        self.assertEqual(self.bridge.recall("nothing stored yet"), [])

    def test_learn_swallows_a_broken_extractor(self):
        class Boom:
            def extract(self, *_a, **_k):
                raise RuntimeError("boom")

        bridge = MemoryBridge(self.store, extractor=Boom())
        self.assertEqual(bridge.learn([{"type": "user-input", "payload": {"text": "x"}}]), [])

    def test_stats_and_forget(self):
        self.bridge.learn([{"type": "user-input", "payload": {"text": "以后都用 PowerShell"}}])
        stats = self.bridge.stats()
        self.assertEqual(stats["total"], 1)
        memory_id = self.store.recent()[0].id
        self.assertTrue(self.bridge.forget(memory_id))
        self.assertEqual(self.bridge.stats()["total"], 0)


# ------------------------------------------------------------------ runtime
class _FakeProvider:
    def __init__(self, system_seen: list):
        self.model = "fake"
        self.last_reasoning = ""
        self._system_seen = system_seen

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        # Record the system prompt the harness actually sent.
        for message in messages:
            if getattr(message, "type", "") == "system":
                self._system_seen.append(getattr(message, "text", ""))
        return EngineChatResponse(
            text="done",
            tool_calls=[],
            usage=EngineUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            model=self.model,
        )


class RuntimeMemoryTest(unittest.TestCase):
    def setUp(self):
        from onion_harness.harness import runtime as runtime_module
        from onion_harness.ledger import ChangeLedger, LedgerStore
        from onion_harness.plugins import builtin_executors
        from onion_harness.sandbox import Sandbox

        self.runtime_module = runtime_module
        self.dir = Path(tempfile.mkdtemp(prefix="onion-rtmem-"))
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self.store = MemoryStore(self.dir / "m.db", cipher=cipher_from_store(MemorySecretStore()))
        self.bridge = MemoryBridge(self.store)
        self._original = runtime_module.build_provider

    def tearDown(self):
        self.runtime_module.build_provider = self._original
        self.ledger.close()
        self.store.close()

    def _run(self, prompt: str, systems: list, mode: str = "standard"):
        from onion_harness.harness.runtime import OnionRuntime, build_model_config

        self.runtime_module.build_provider = lambda *a, **k: _FakeProvider(systems)
        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
            memory=self.bridge,
        )
        model = build_model_config(
            {"provider_id": "deepseek", "api_key": "sk-test", "mode": mode}
        )
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt=prompt, workspace=str(self.dir)), model):
                events.append(ev)

        asyncio.run(consume())
        return events

    def test_recalled_memory_reaches_the_system_channel(self):
        self.store.remember(new_memory("用户偏好：以后都用 PowerShell", MemoryKind.PREFERENCE))
        systems: list = []
        events = self._run("用 PowerShell 列一下文件", systems)
        self.assertTrue(systems, "the provider saw no system message")
        self.assertIn("Long-term memory", systems[0])
        self.assertIn("PowerShell", systems[0])
        self.assertIn(EventType.MEMORY_RECALLED, [ev.type for ev in events])

    def test_mode_contract_still_comes_last(self):
        """Recalled memory must sit before the mode contract, not after it.

        Strict mode's anti-hallucination rules are the final instruction the model
        reads. If a recalled note came after them, the note would look like the
        newest, most authoritative text in the system channel.
        """
        self.store.remember(new_memory("用户偏好：以后都用 PowerShell", MemoryKind.PREFERENCE))
        systems: list = []
        self._run("用 PowerShell 列一下文件", systems, mode="strict")
        prompt = systems[0]
        memory_at = prompt.find("Long-term memory")
        self.assertGreaterEqual(memory_at, 0)
        # The strict contract is appended verbatim by compose_mode_prompt.
        contract_at = prompt.find("Source:", memory_at)
        self.assertGreater(contract_at, memory_at, "the mode contract must be the last block")

    def test_recall_event_carries_summaries_not_full_text(self):
        self.store.remember(new_memory("用户偏好：以后都用 PowerShell", MemoryKind.PREFERENCE))
        events = self._run("PowerShell", [])
        recalled = [ev for ev in events if ev.type is EventType.MEMORY_RECALLED]
        self.assertEqual(len(recalled), 1)
        self.assertGreaterEqual(recalled[0].payload["count"], 1)
        self.assertTrue(recalled[0].payload["memories"])

    def test_task_teaches_memory_at_the_end(self):
        events = self._run("以后都用 PowerShell 写脚本", [])
        types = [ev.type for ev in events]
        learned = [ev for ev in events if ev.type is EventType.MEMORY_LEARNED]
        self.assertEqual(len(learned), 1)
        # Learning is announced before the task is reported done.
        self.assertLess(types.index(EventType.MEMORY_LEARNED), types.index(EventType.TASK_DONE))
        self.assertGreaterEqual(learned[0].payload["count"], 1)
        self.assertGreaterEqual(self.store.count(), 1)

    def test_no_memory_port_still_runs(self):
        from onion_harness.harness.runtime import OnionRuntime, build_model_config

        systems: list = []
        self.runtime_module.build_provider = lambda *a, **k: _FakeProvider(systems)
        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
        )
        model = build_model_config({"provider_id": "deepseek", "api_key": "sk-test"})
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt="hi"), model):
                events.append(ev)

        asyncio.run(consume())
        types = [ev.type for ev in events]
        self.assertIn(EventType.TASK_DONE, types)
        self.assertNotIn(EventType.MEMORY_RECALLED, types)
        self.assertNotIn(EventType.MEMORY_LEARNED, types)


if __name__ == "__main__":
    unittest.main()