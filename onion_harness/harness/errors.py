"""Error boundary - nothing upstream-shaped escapes the harness.

Two jobs:

* sanitise any text before it reaches the GUI, the event stream or a log line,
  including the engine's package name and its class prefixes;
* turn provider/transport failures into short, human-readable messages without
  dumping raw tracebacks into the conversation.
"""

from __future__ import annotations

import re
import traceback
from typing import Any, Optional

from onion_harness.harness.bridge import engine_module_name, engine_root_name

_PACKAGE = engine_module_name()
_ROOT = engine_root_name()

_PATTERNS = [
    (re.compile(re.escape(_PACKAGE)), "onion.runtime"),
    (re.compile(re.escape(_PACKAGE.replace("_", "-"))), "onion.runtime"),
    (re.compile(re.escape(_ROOT) + r"\."), "onion.runtime."),
    (re.compile(r"\bLoongAgent"), "OnionRuntime"),
    (re.compile(r"\bLoong\b"), "Onion"),
]


class BoundaryError(RuntimeError):
    """An error safe to surface to the user and the UI."""

    def __init__(self, message: str, *, code: str = "runtime-error", detail: str = "") -> None:
        super().__init__(sanitize(message))
        self.public_message = sanitize(message)
        self.code = code
        self.detail = sanitize(detail)

    def to_dict(self) -> dict:
        return {"message": self.public_message, "code": self.code}


def sanitize(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_logger_name(name: str) -> str:
    if name == _PACKAGE or name.startswith(_PACKAGE + "."):
        return "onion.runtime" + name[len(_PACKAGE) :]
    return name


def project_traceback(exc: BaseException, max_frames: int = 6) -> str:
    """A traceback limited to Onion frames, with package names scrubbed."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "onion_harness" in (f.filename or "")]
    chosen = ours[-max_frames:] if ours else frames[-max_frames:]
    rendered = "".join(traceback.format_list(chosen))
    return sanitize(rendered).strip()


def friendly_provider_error(exc: BaseException) -> BoundaryError:
    """Map transport/provider failures onto concise, non-leaky messages."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    body = ""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.text or ""
        except Exception:  # noqa: BLE001
            body = ""

    message = _extract_api_message(body) or sanitize(str(exc)) or type(exc).__name__

    if status in (401, 403):
        return BoundaryError(
            f"Authentication failed ({status}). Check the API key for this provider.",
            code="auth",
            detail=message,
        )
    if status == 404:
        return BoundaryError(
            "Endpoint or model not found (404). Check the base URL and model name.",
            code="not-found",
            detail=message,
        )
    if status == 429:
        return BoundaryError(
            "Rate limited by the provider (429). Wait a moment and retry.",
            code="rate-limit",
            detail=message,
        )
    if status and int(status) >= 500:
        return BoundaryError(
            f"Provider is unavailable ({status}). Retry shortly.",
            code="provider-unavailable",
            detail=message,
        )
    if status:
        return BoundaryError(
            f"Model request failed ({status}): {message}", code="provider", detail=message
        )
    if _looks_like_tls(exc):
        return BoundaryError(
            "TLS certificate verification failed. Something on this machine is "
            "intercepting HTTPS (antivirus or a corporate proxy) and its root "
            "certificate is not trusted. Install that root in the OS store, or "
            "allow this app through the interceptor.",
            code="tls",
            detail=sanitize(str(exc)),
        )
    if _looks_like_network(exc):
        return BoundaryError(
            "Could not reach the model provider. Check the base URL and your network.",
            code="network",
            detail=sanitize(str(exc)),
        )
    return BoundaryError(
        f"Model request failed: {message}", code="provider", detail=sanitize(str(exc))
    )


def _extract_api_message(body: str) -> Optional[str]:
    if not body:
        return None
    import json

    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return body[:400].strip() or None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "")[:400] or None
        if isinstance(error, str):
            return error[:400]
        if data.get("message"):
            return str(data["message"])[:400]
    return None


def _looks_like_tls(exc: BaseException) -> bool:
    """True when the failure is certificate validation, not reachability.

    httpx reports certificate problems as a ConnectError wrapping SSLError, so
    checking only the exception name would misreport them as a network outage.
    """
    text = f"{type(exc).__name__} {exc} {getattr(exc, '__cause__', '')}".lower()
    return "certificate" in text or "ssl" in text


def _looks_like_network(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return any(token in name for token in ("connect", "timeout", "network", "ssl"))


__all__ = [
    "BoundaryError",
    "sanitize",
    "sanitize_logger_name",
    "project_traceback",
    "friendly_provider_error",
]