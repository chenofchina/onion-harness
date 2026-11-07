"""Thread-safe event bus bridging the asyncio runtime and HTTP handlers.

The runtime publishes from its own event loop; SSE handlers read from request
threads.  A bus decouples the two and gives late subscribers a replay.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List, Optional


class Subscription:
    def __init__(self, task_id: Optional[str] = None) -> None:
        self.task_id = task_id
        self.queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=2048)

    def put(self, payload: dict) -> None:
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            # Slow consumer: drop the oldest event rather than block the runtime.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(payload)
            except queue.Empty:
                pass

    def close(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass

    def get(self, timeout: float = 15.0) -> Optional[dict]:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return {}


class EventBus:
    """Fan-out event bus with per-task replay buffers."""

    def __init__(self, history_limit: int = 2000) -> None:
        self._lock = threading.RLock()
        self._subscribers: List[Subscription] = []
        self._replay: Dict[str, List[dict]] = {}
        self._history_limit = history_limit
        self._last_seq: Dict[str, int] = {}

    def publish(self, payload: Dict[str, Any]) -> None:
        task_id = payload.get("task_id", "")
        with self._lock:
            buffer = self._replay.setdefault(task_id, [])
            buffer.append(payload)
            if len(buffer) > self._history_limit:
                del buffer[: len(buffer) - self._history_limit]
            subscribers = list(self._subscribers)
        for sub in subscribers:
            if sub.task_id is None or sub.task_id == task_id:
                sub.put(payload)

    def subscribe(self, task_id: Optional[str] = None, replay: bool = True) -> Subscription:
        sub = Subscription(task_id)
        with self._lock:
            if replay and task_id:
                for payload in self._replay.get(task_id, []):
                    sub.put(payload)
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)
        sub.close()

    def replay_for(self, task_id: str) -> List[dict]:
        with self._lock:
            return list(self._replay.get(task_id, []))

    def reset_task(self, task_id: str) -> None:
        with self._lock:
            self._replay.pop(task_id, None)


def sse_format(payload: Dict[str, Any]) -> bytes:
    """Encode one payload as an SSE frame."""
    import json

    body = json.dumps(payload, ensure_ascii=False, default=str)
    event_id = payload.get("id") or payload.get("seq") or int(time.time() * 1000)
    return f"id: {event_id}\ndata: {body}\n\n".encode("utf-8")


__all__ = ["EventBus", "Subscription", "sse_format"]