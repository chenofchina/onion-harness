"""Durable task history and settings for the API layer.

Layout under the data directory::

    data/
      settings.json          # app settings; holds key *names*, never key values
      tasks.json             # task metadata index
      tasks/<task_id>.jsonl  # full event stream per task
      ledger.sqlite3         # change ledger
      memory.sqlite3         # encrypted long-term memory

The API layer owns these files; the harness and inner layers know nothing about
them.

API keys never appear in ``settings.json``. They live in the OS credential
manager and the file only records which providers have one (``key_providers``),
because a provider id is not a secret and the GUI needs the list to show which
rows are configured.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from onion_harness.api.secrets import (
    KeyVault,
    MemorySecretStore,
    SecretError,
    SecretStore,
    default_secret_store,
)

logger = logging.getLogger("onion.store")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme_mode": "system",
    "provider_id": "deepseek",
    "model": "deepseek-chat",
    "thinking": "off",
    "workspace": "",
    "max_turns": 40,
    "keep_recent_messages": 10,
    # Model context window in tokens. 128k is a safe default for most
    # providers; the GUI offers a slider up to 1M.
    "context_window": 128000,
    # Per-provider base URL overrides. A provider preset supplies the default;
    # this map holds whatever the user typed, including for "custom" endpoints.
    "base_urls": {},
    "mode": "standard",
    "elevation": "standard",
    # Provider ids that hold a key in the credential store. Values live there.
    "key_providers": [],
}

# Legacy fields stripped on sight: an older build wrote key values into the file.
_SECRET_FIELDS = ("api_key", "api_keys")

_memory_stores: Dict[str, MemorySecretStore] = {}


def _looks_like_a_test_run() -> bool:
    """True when this process is a test runner.

    Two reasons this matters. Tests use throwaway keys like ``"sk-test"``; writing
    those to the real credential store would overwrite the developer's actual
    provider keys, and the damage would be invisible until the next real request
    failed to authenticate. The check is process-wide because the conftest hook
    that sets ``ONION_SECRET_STORE`` is bypassed by ``python -m unittest``.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get("ONION_SECRET_STORE", "").strip().lower() == "memory":
        return True
    if "pytest" in sys.modules or "unittest" in sys.modules:
        return True
    argv0 = Path(sys.argv[0] or "").name.lower()
    return argv0 in {"pytest", "py.test", "unittest"}


def _default_store(root: Path) -> SecretStore:
    """The credential store to use, honouring an explicit override.

    ``ONION_SECRET_STORE=memory`` exists for tests and portable runs. As a second
    line of defence the process is inspected for a test runner, so a test module
    that forgets the environment variable still cannot touch the real store. In
    that mode the store is keyed by data directory so two tests never see each
    other's keys.
    """
    if _looks_like_a_test_run():
        key = str(root)
        if key not in _memory_stores:
            _memory_stores[key] = MemorySecretStore()
        return _memory_stores[key]
    try:
        return default_secret_store()
    except SecretError as exc:
        # Keys cannot be persisted, but keeping them in memory is still better
        # than writing them to disk: they survive the session, never the file.
        logger.warning("credential store unavailable, keys are session-only: %s", exc)
        key = str(root)
        if key not in _memory_stores:
            _memory_stores[key] = MemorySecretStore()
        return _memory_stores[key]


class AppStore:
    """Small JSON-backed store; atomic writes, guarded by a lock."""

    def __init__(self, data_dir: str | Path, vault: Optional[KeyVault] = None) -> None:
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tasks").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._vault = vault or KeyVault(_default_store(self.root))

    @property
    def vault(self) -> KeyVault:
        return self._vault

    # -- settings -----------------------------------------------------------
    def load_settings(self) -> Dict[str, Any]:
        path = self.root / "settings.json"
        with self._lock:
            if not path.exists():
                return dict(DEFAULT_SETTINGS)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return dict(DEFAULT_SETTINGS)
        settings = dict(DEFAULT_SETTINGS)
        settings.update(raw if isinstance(raw, dict) else {})
        return settings

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings)
        # Belt and braces: no key value may ever reach the file, whatever a
        # caller put into the dict.
        for field in _SECRET_FIELDS:
            merged.pop(field, None)
        merged["key_providers"] = sorted(
            {str(pid) for pid in (merged.get("key_providers") or []) if str(pid).strip()}
        )
        path = self.root / "settings.json"
        with self._lock:
            _atomic_write_json(path, merged)
        return merged

    def public_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Strip secrets before anything crosses the HTTP boundary."""
        public = {k: v for k, v in settings.items() if k not in _SECRET_FIELDS}
        # Only report providers whose key is actually retrievable, so a stale
        # name left behind after a manual credential deletion does not make the
        # GUI claim a provider is configured.
        public["configured_providers"] = [
            pid for pid in (settings.get("key_providers") or []) if self._vault.has(pid)
        ]
        # Overrides are not secret, and the Advanced drawer needs them to prefill
        # whatever the user typed for a provider or custom endpoint.
        public["base_urls"] = dict(settings.get("base_urls", {}) or {})
        return public

    # -- keys ---------------------------------------------------------------
    def provider_keys(self, settings: Dict[str, Any]) -> Dict[str, str]:
        """The actual key values, read from the credential store on demand.

        Nothing caches them: a key revoked in the OS UI takes effect immediately.
        """
        out: Dict[str, str] = {}
        for provider_id in settings.get("key_providers") or []:
            value = self._vault.get(str(provider_id))
            if value:
                out[str(provider_id)] = value
        return out

    def set_provider_key(
        self, settings: Dict[str, Any], provider_id: str, api_key: str
    ) -> Dict[str, Any]:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            return settings
        self._vault.set(provider_id, api_key)
        return self._mark(settings, provider_id, self._vault.has(provider_id))

    def clear_provider_key(self, settings: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
        provider_id = str(provider_id or "").strip()
        if provider_id:
            self._vault.delete(provider_id)
            settings = self._mark(settings, provider_id, False)
        return settings

    def _mark(self, settings: Dict[str, Any], provider_id: str, present: bool) -> Dict[str, Any]:
        names = {str(pid) for pid in (settings.get("key_providers") or [])}
        if present:
            names.add(provider_id)
        else:
            names.discard(provider_id)
        settings = dict(settings)
        settings["key_providers"] = sorted(names)
        return settings

    def migrate_plaintext_keys(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Move keys out of ``settings.json`` and into the credential store.

        Runs on every start; on an already-migrated file it is a no-op. Keys are
        written to the vault *before* the file is rewritten, so a crash in
        between duplicates work rather than losing a key.
        """
        legacy = settings.get("api_keys") or {}
        if not isinstance(legacy, dict) or not legacy:
            return settings

        moved = 0
        for provider_id, value in legacy.items():
            text = str(value or "").strip()
            if not text:
                continue
            try:
                self._vault.set(str(provider_id), text)
            except SecretError as exc:
                logger.error("could not migrate the key for %s: %s", provider_id, exc)
                continue
            settings = self._mark(settings, str(provider_id), True)
            moved += 1

        settings = dict(settings)
        settings.pop("api_keys", None)
        saved = self.save_settings(settings)
        if moved:
            logger.info("migrated %d API key(s) out of settings.json", moved)
        return saved

    # -- tasks --------------------------------------------------------------
    def load_tasks(self) -> List[Dict[str, Any]]:
        path = self.root / "tasks.json"
        with self._lock:
            if not path.exists():
                return []
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
        return raw if isinstance(raw, list) else []

    def save_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        with self._lock:
            _atomic_write_json(self.root / "tasks.json", tasks)

    def upsert_task(self, record: Dict[str, Any]) -> None:
        with self._lock:
            tasks = self.load_tasks()
            for index, existing in enumerate(tasks):
                if existing.get("id") == record.get("id"):
                    tasks[index] = {**existing, **record}
                    break
            else:
                tasks.insert(0, record)
            self.save_tasks(tasks)

    def append_event(self, task_id: str, payload: Dict[str, Any]) -> None:
        path = self.root / "tasks" / f"{_safe(task_id)}.jsonl"
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def load_events(self, task_id: str) -> List[Dict[str, Any]]:
        path = self.root / "tasks" / f"{_safe(task_id)}.jsonl"
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with self._lock:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self.save_tasks([t for t in self.load_tasks() if t.get("id") != task_id])
            path = self.root / "tasks" / f"{_safe(task_id)}.jsonl"
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:80]


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def default_data_dir() -> Path:
    """The canonical Onion home directory.

    ``~/.onionharness`` per the v2 spec; ``ONION_DATA_DIR`` overrides it for
    tests and portable installs.
    """
    env = os.environ.get("ONION_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".onionharness"


__all__ = ["AppStore", "DEFAULT_SETTINGS", "default_data_dir"]