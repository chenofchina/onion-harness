"""Automatic safety review before a destructive tool call reaches the sandbox.

This is one extra gate in front of the sandbox, never a replacement for it. The
sandbox still decides what a command may touch; the reviewer only decides whether
the command is obviously dangerous enough that a human should look before it runs
at all.

Two failure modes drive the design:

* **False negative** - the reviewer says PASS but the code is harmful. No static
  or model review catches everything, which is why the sandbox stays in place.
* **False positive** - the reviewer rejects ordinary work, the user turns the
  feature off, and the protection becomes zero. So only *destructive* snippets are
  sent for review at all; reads and harmless writes never pay the latency.

When the reviewer cannot be reached (no key, timeout, malformed answer) the call
is refused, but the message says the review was unavailable - not that the code
was dangerous. Confusing those two teaches users to distrust a working gate.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("onion.review")

DECISION_PASS = "pass"
DECISION_REJECT = "reject"
DECISION_UNAVAILABLE = "unavailable"

# A snippet matching none of these is treated as read-only and skips review.
# Deliberately broad: a false positive here costs one cheap review call, while a
# false negative let a destructive command through unreviewed.
_DESTRUCTIVE_MARKERS = (
    # file destruction
    "rm ", "rmdir", "del ", "erase ", "remove-item", "ri ",
    "format ", "mkfs", "diskpart", "cipher /w",
    # overwrite / move across trees
    "move-item", "mv ", "rename-item",
    "set-content", "out-file", ">", ">>",
    # registry and services
    "reg add", "reg delete", "sc config", "sc delete", "net user", "net localgroup",
    "schtasks", "set-service", "new-service",
    # permissions
    "icacls", "takeown", "chmod", "chown", "attrib",
    # system-level
    "shutdown", "restart-computer", "stop-computer", "bcdedit",
    "start-process", "invoke-expression", "iex ",
    "curl ", "wget ", "invoke-webrequest", "iwr ",
    "pip install", "npm install", "install-module",
    # git history rewriting
    "git reset --hard", "git clean -", "git push --force", "git push -f",
)

# Read-only verbs. If a snippet contains one of these and no destructive marker,
# review is skipped even though the text may mention words like "delete" inside a
# string. This is a cheap sanity check, not a parser.
_READ_ONLY_HINTS = (
    "get-childitem", "get-content", "get-item", "get-process",
    "ls", "dir", "cat", "type ", "head ", "tail ", "grep", "findstr",
    "select-string", "where.exe", "which ",
)

_PROMPT = """You are a code safety reviewer. Decide whether the code below contains \
HIGH-RISK operations: deleting or overwriting files, formatting disks, writing the \
registry, changing permissions, sending data to the network, executing downloaded \
content, modifying system directories, or bulk renaming.

Answer with exactly one word: PASS or REJECT.
If you are unsure, answer REJECT.

Code:
```
{code}
```
"""

_ANSWER_RE = re.compile(r"\b(PASS|REJECT)\b", re.IGNORECASE)
_MAX_REVIEW_CHARS = 6000


@dataclass
class ReviewConfig:
    """How the automatic review behaves. Editable from settings."""

    enabled: bool = True
    provider_id: str = "deepseek"
    model: str = "deepseek-flash"
    timeout_seconds: float = 12.0
    # When the reviewer cannot answer, refuse the call. Turning this off means an
    # unreachable reviewer silently becomes no reviewer, which is exactly the
    # failure the setting exists to avoid.
    fail_closed: bool = True

    def to_public(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider_id": self.provider_id,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "fail_closed": self.fail_closed,
        }

    @classmethod
    def from_public(cls, raw: Any) -> "ReviewConfig":
        base = cls()
        if not isinstance(raw, dict):
            return base
        try:
            timeout = float(raw.get("timeout_seconds", base.timeout_seconds))
        except (TypeError, ValueError):
            timeout = base.timeout_seconds
        return cls(
            enabled=bool(raw.get("enabled", base.enabled)),
            provider_id=str(raw.get("provider_id") or base.provider_id),
            model=str(raw.get("model") or base.model),
            # A short timeout keeps a hung reviewer from stalling the task; a
            # huge one would let a single tool call block for minutes.
            timeout_seconds=max(2.0, min(timeout, 60.0)),
            fail_closed=bool(raw.get("fail_closed", base.fail_closed)),
        )


@dataclass
class ReviewResult:
    """The outcome of one review, shaped for both the gate and the event stream."""

    decision: str
    reason: str = ""
    reviewer_model: str = ""
    duration_ms: int = 0
    skipped: bool = False
    # True when the decision was made *because the reviewer could not answer*,
    # not because the code looked dangerous. The two cases need different words,
    # and a flag makes that a fact rather than a string-matching guess.
    unavailable: bool = False

    @property
    def approved(self) -> bool:
        return self.decision == DECISION_PASS

    def blocked_message(self) -> str:
        """Text handed back to the model when the call is refused.

        The two cases are worded differently on purpose: the model (and the user
        reading the transcript) must be able to tell "this looks dangerous" from
        "the checker was down".
        """
        if self.unavailable:
            return (
                "Blocked because the automatic safety review could not run "
                f"({self.reason}). This is a problem with the review service, not "
                "evidence that the code is dangerous. Tell the user the reviewer is "
                "unavailable; do not retry the same call."
            )
        return (
            "Blocked by automatic safety review: this snippet appears to perform a "
            "destructive or high-risk operation. It was not run. Explain what you "
            "intended and ask the user how to proceed."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "reviewer_model": self.reviewer_model,
            "duration_ms": self.duration_ms,
            "skipped": self.skipped,
            "unavailable": self.unavailable,
        }


def looks_destructive(code: str) -> bool:
    """Does this snippet warrant a review call?

    Writes and shell side effects qualify; pure reads do not. The check errs
    toward reviewing, because the alternative is unreviewed destruction.
    """
    lowered = (code or "").lower()
    if not lowered.strip():
        return False
    if any(marker in lowered for marker in _DESTRUCTIVE_MARKERS):
        return True
    # A write verb with no destructive marker still counts: "New-Item" creates.
    from onion_harness.core.modes import looks_like_write

    if looks_like_write(lowered):
        # Reads that merely mention a write word in a string are still reads.
        if any(hint in lowered for hint in _READ_ONLY_HINTS):
            return False
        return True
    return False


def parse_answer(text: str) -> Optional[str]:
    """Extract PASS/REJECT from a reviewer reply.

    Only the first match counts. A reply containing both ("PASS, though...")
    resolves to whichever came first, and anything unrecognised returns None so
    the caller applies its fail-closed rule instead of guessing.
    """
    match = _ANSWER_RE.search(text or "")
    if not match:
        return None
    return DECISION_PASS if match.group(1).upper() == "PASS" else DECISION_REJECT


class ReviewGate:
    """Runs the reviewer and returns a decision. Never raises to the caller."""

    def __init__(self, config: ReviewConfig, provider_factory=None) -> None:
        self.config = config
        # Injected in tests; the application supplies a factory that builds a
        # provider from the resolved model configuration.
        self._provider_factory = provider_factory
        self._provider = None
        self._lock = threading.Lock()

    def update_config(self, config: ReviewConfig) -> None:
        with self._lock:
            self.config = config
            # The model may have changed, so drop the cached provider.
            self._provider = None

    async def review(self, code: str, *, tool_name: str = "") -> ReviewResult:
        """Decide whether ``code`` may run. Never raises.

        Async because the tool handler that calls it is async: building a
        provider on one event loop and driving it from another (the obvious
        thread-based shortcut) breaks the underlying HTTP client.
        """
        if not self.config.enabled:
            return ReviewResult(DECISION_PASS, reason="review disabled", skipped=True)
        if not looks_destructive(code):
            return ReviewResult(DECISION_PASS, reason="read-only snippet", skipped=True)
        if not (code or "").strip():
            return ReviewResult(DECISION_PASS, reason="empty snippet", skipped=True)
        return await self._ask(code, tool_name=tool_name)

    async def _ask(self, code: str, *, tool_name: str) -> ReviewResult:
        import time

        started = time.time()
        snippet = code if len(code) <= _MAX_REVIEW_CHARS else code[:_MAX_REVIEW_CHARS]
        prompt = _PROMPT.format(code=snippet)

        try:
            text = await self._call(prompt)
        except Exception as exc:  # noqa: BLE001 - any failure is "unavailable"
            logger.warning("safety review unavailable: %s", exc)
            return self._unavailable(str(exc), started)

        decision = parse_answer(text)
        if decision is None:
            return self._unavailable("the reviewer returned an unusable answer", started)

        return ReviewResult(
            decision=decision,
            reason=(text or "").strip()[:300],
            reviewer_model=self.config.model,
            duration_ms=int((time.time() - started) * 1000),
        )

    def _unavailable(self, reason: str, started: float) -> ReviewResult:
        import time

        decision = DECISION_REJECT if self.config.fail_closed else DECISION_PASS
        return ReviewResult(
            decision=decision,
            reason=f"reviewer unavailable: {reason}",
            reviewer_model=self.config.model,
            duration_ms=int((time.time() - started) * 1000),
            unavailable=True,
        )

    # -- transport ----------------------------------------------------------
    async def _call(self, prompt: str) -> str:
        """One reviewer request with a hard timeout."""
        import asyncio

        from onion_harness.harness.bridge import EngineMessage

        if self._provider_factory is None:
            raise RuntimeError("no reviewer provider configured")

        with self._lock:
            if self._provider is None:
                self._provider = self._provider_factory(self.config)
            provider = self._provider

        messages = [EngineMessage.user(text=prompt)]
        response = await asyncio.wait_for(
            provider.chat_completion(messages, tools=None, temperature=0.0),
            timeout=self.config.timeout_seconds,
        )
        return str(getattr(response, "text", "") or "")

    def reset(self) -> None:
        with self._lock:
            self._provider = None


def build_gate_from_model(model_factory, config: ReviewConfig) -> ReviewGate:
    """Convenience: a gate whose provider comes from a model-config factory."""
    return ReviewGate(config, provider_factory=model_factory)


__all__ = [
    "ReviewConfig",
    "ReviewResult",
    "ReviewGate",
    "build_gate_from_model",
    "looks_destructive",
    "parse_answer",
    "DECISION_PASS",
    "DECISION_REJECT",
    "DECISION_UNAVAILABLE",
]