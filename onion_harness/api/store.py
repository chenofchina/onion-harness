"""Durable task history and settings for the API layer.

Layout under the data directory::

    data/
      settings.json          # app settings incl. per-provider API keys
      tasks.json             # task metadata index
      tasks/<task_id>.jsonl  # full event stream per task
      ledger.sqlite3         # change ledger

The API layer owns these files; the harness and inner layers know nothing about
them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

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
    "api_keys": {},
}

_SECRET_FIELDS = ("api_key", "api_keys")


class AppStore:
    """Small JSON-backed store; atomic writes, guarded by a lock."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tasks").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

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
        path = self.root / "settings.json"
        with self._lock:
            _atomic_write_json(path, merged)
        return merged

    def public_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Strip secrets before anything crosses the HTTP boundary."""
        public = {k: v for k, v in settings.items() if k not in _SECRET_FIELDS}
        keys = settings.get("api_keys", {}) or {}
        public["configured_providers"] = [pid for pid, value in keys.items() if value]
        # Overrides are not secret, and the Advanced drawer needs them to prefill
        # whatever the user typed for a provider or custom endpoint.
        public["base_urls"] = dict(settings.get("base_urls", {}) or {})
        return public

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