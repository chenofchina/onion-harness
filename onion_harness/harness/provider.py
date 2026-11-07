"""Model providers.

Two wire formats are supported:

* **OpenAI-compatible** - the upstream engine's provider, extended so the
  reasoning text some vendors return (``reasoning_content`` / ``reasoning``) is
  captured instead of silently dropped.
* **Anthropic** - a hand-written adapter that translates the engine's neutral
  message list into the Messages API and translates the response back into the
  same neutral shape.  This is the only place Anthropic-specific JSON exists.

Both are wrapped by :class:`CompressingProvider`, which trims old context
before every request while leaving system messages untouched.
"""

from __future__ import annotations

import json
import ssl
import uuid
from typing import Any, Dict, List, Optional

import httpx

from onion_harness.harness.bridge import (
    EngineChatResponse,
    EngineOpenAIProvider,
    EngineProviderBase,
    EngineToolCall,
    EngineUsage,
    require_engine,
)
from onion_harness.harness.context import ContextCompressor, estimate_tokens
from onion_harness.harness.errors import BoundaryError, friendly_provider_error


def system_ssl_context():
    """TLS context backed by the OS certificate store.

    Python ships its own CA bundle (certifi), which does not include roots added
    to Windows/macOS by antivirus or corporate proxies. Those proxies terminate
    TLS with a locally-installed root, so requests fail with
    CERTIFICATE_VERIFY_FAILED even though the network is fine. Using the system
    store trusts exactly what the OS trusts.
    """
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - fall back to the default bundle
        return ssl.create_default_context()


def http_client(**kwargs: Any) -> httpx.AsyncClient:
    """An httpx client that verifies against the system trust store."""
    kwargs.setdefault("verify", system_ssl_context())
    return httpx.AsyncClient(**kwargs)


class ReasoningCapture:
    """Mixin storing the most recent reasoning text for the harness to emit."""

    last_reasoning: str = ""

    def _capture_reasoning(self, text: str) -> None:
        self.last_reasoning = (text or "").strip()


class OnionOpenAIProvider(ReasoningCapture, EngineOpenAIProvider):
    """OpenAI-compatible provider that keeps vendor reasoning content."""

    provider_id: str = "openai"

    def __init__(self, *args: Any, reasoning_effort: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reasoning_effort = reasoning_effort
        # The base class builds its own httpx client with certifi's bundle. Swap
        # it for one that trusts the OS store, otherwise any TLS-terminating
        # antivirus or corporate proxy makes every request fail.
        try:
            old_client = getattr(self, "_client", None)
            self._client = http_client(
                timeout=httpx.Timeout(self.timeout, connect=self.timeout),
                headers={"Authorization": f"Bearer {self.api_key}", **self.extra_headers},
            )
            if old_client is not None:
                try:
                    old_client.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - keep the base client on failure
            pass

    def _parse_response(self, data: Dict[str, Any]):  # noqa: D401 - override
        response = super()._parse_response(data)
        try:
            message = ((data.get("choices") or [{}])[0].get("message")) or {}
            reasoning = (
                message.get("reasoning_content")
                or message.get("reasoning")
                or _join_reasoning_details(message.get("reasoning_details"))
            )
            if reasoning:
                self._capture_reasoning(str(reasoning))
            else:
                self.last_reasoning = ""
        except Exception:  # noqa: BLE001 - never fail a call over telemetry
            self.last_reasoning = ""
        return response

    async def chat_completion(self, messages, tools=None, **kwargs):
        if self._reasoning_effort and self._reasoning_effort != "off":
            kwargs.setdefault("reasoning_effort", self._reasoning_effort)
        return await super().chat_completion(messages, tools=tools, **kwargs)

    def clone(self, model: Optional[str] = None) -> "OnionOpenAIProvider":
        return OnionOpenAIProvider(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model or self.model,
            timeout=self.timeout,
            temperature=self.temperature,
            extra_headers=dict(self.extra_headers),
            reasoning_effort=self._reasoning_effort,
            **self.extra_kwargs,
        )


class AnthropicProvider(ReasoningCapture, EngineProviderBase):
    """Anthropic Messages API adapter exposing the neutral provider interface."""

    provider_id = "anthropic"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        temperature: Optional[float] = None,
        max_tokens: int = 8192,
        thinking_budget: int = 0,
        extra_headers: Optional[Dict[str, str]] = None,
        api_version: str = "2023-06-01",
    ) -> None:
        require_engine()
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking_budget = max(0, thinking_budget)
        self.api_version = api_version
        self.extra_headers = extra_headers or {}
        self._client = http_client(
            timeout=httpx.Timeout(timeout, connect=min(30.0, timeout)),
            headers={
                "x-api-key": api_key,
                "anthropic-version": api_version,
                "content-type": "application/json",
                **self.extra_headers,
            },
        )

    def get_model_name(self) -> str:
        return self.model

    def clone(self, model: Optional[str] = None) -> "AnthropicProvider":
        return AnthropicProvider(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model or self.model,
            timeout=self.timeout,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            thinking_budget=self.thinking_budget,
            extra_headers=dict(self.extra_headers),
            api_version=self.api_version,
        )

    async def chat_completion(
        self, messages, tools=None, **kwargs: Any
    ):
        system, converted = self._to_anthropic_messages(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": int(kwargs.pop("max_tokens", self.max_tokens)),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [self._to_anthropic_tool(t) for t in tools]
            payload["tool_choice"] = {"type": "auto"}

        budget = int(kwargs.pop("thinking_budget", self.thinking_budget) or 0)
        if budget > 0:
            # Extended thinking requires headroom above the thinking budget and
            # is incompatible with an explicit temperature.
            payload["max_tokens"] = max(payload["max_tokens"], budget + 4096)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            temperature = kwargs.pop("temperature", self.temperature)
            if temperature is not None:
                payload["temperature"] = temperature

        url = f"{self.base_url}/messages"
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - normalise at the boundary
            friendly = friendly_provider_error(exc)
            raise BoundaryError(friendly.public_message, code=friendly.code, detail=friendly.detail) from None

        return self._parse_anthropic_response(data)

    # -- request translation -------------------------------------------------
    def _to_anthropic_messages(self, messages) -> tuple[str, List[Dict[str, Any]]]:
        system_parts: List[str] = []
        converted: List[Dict[str, Any]] = []

        for message in messages:
            mtype = getattr(message, "type", None)
            if mtype == "system":
                text = getattr(message, "text", "") or ""
                if text:
                    system_parts.append(text)
                continue
            if mtype == "result":
                continue

            if mtype == "assistant":
                blocks: List[Dict[str, Any]] = []
                text = getattr(message, "text", "") or ""
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                for call in getattr(message, "tool_calls", []) or []:
                    call_id = getattr(call, "id", "") or f"toolu_{uuid.uuid4().hex[:16]}"
                    call.id = call_id
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": getattr(call, "name", ""),
                            "input": getattr(call, "arguments", {}) or {},
                        }
                    )
                if blocks:
                    _append_block(converted, "assistant", blocks)
            elif mtype == "user":
                blocks = []
                for result in getattr(message, "tool_results", []) or []:
                    block: Dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": getattr(result, "tool_use_id", "") or "unknown",
                        "content": getattr(result, "content", "") or "",
                    }
                    if getattr(result, "is_error", False):
                        block["is_error"] = True
                    blocks.append(block)
                text = getattr(message, "text", "") or ""
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                if blocks:
                    _append_block(converted, "user", blocks)

        if not converted:
            converted.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
        if converted[0]["role"] != "user":
            converted.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]})

        return "\n\n".join(system_parts).strip(), converted

    @staticmethod
    def _to_anthropic_tool(tool) -> Dict[str, Any]:
        if isinstance(tool, dict):
            return {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
            }
        return {
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", ""),
            "input_schema": getattr(tool, "input_schema", None)
            or {"type": "object", "properties": {}},
        }

    # -- response translation ------------------------------------------------
    def _parse_anthropic_response(self, data: Dict[str, Any]):
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        tool_calls: List[Any] = []

        for block in data.get("content") or []:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text") or "")
            elif block_type == "thinking":
                thinking_parts.append(block.get("thinking") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    EngineToolCall(
                        id=block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                        name=block.get("name") or "",
                        arguments=block.get("input") or {},
                    )
                )

        usage_raw = data.get("usage") or {}
        prompt_tokens = int(usage_raw.get("input_tokens") or 0)
        completion_tokens = int(usage_raw.get("output_tokens") or 0)
        usage = EngineUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        reasoning = "\n\n".join(p for p in thinking_parts if p.strip())
        if reasoning:
            self._capture_reasoning(reasoning)
        else:
            self.last_reasoning = ""

        return EngineChatResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=usage,
            model=data.get("model") or self.model,
            finish_reason=data.get("stop_reason"),
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class HistoryReplayProvider(ReasoningCapture, EngineProviderBase):
    """Prepends prior conversation turns on every request.

    History is inserted *after* any leading system messages, which keeps the
    system prompt first regardless of when the engine appends it. This is what
    makes a follow-up prompt continue the same conversation instead of starting
    cold.
    """

    def __init__(self, inner, history: Optional[list] = None) -> None:
        self._inner = inner
        self._history = [turn for turn in (history or []) if turn.get("text")]
        # The harness reads these off the outermost provider; a decorator that
        # hides them silently breaks compression telemetry.
        self.last_compression: Dict[str, Any] = {}
        self.last_context_tokens = 0

    @property
    def inner(self):
        return self._inner

    @property
    def last_compression(self) -> Dict[str, Any]:
        return getattr(self._inner, "last_compression", {}) or {}

    @last_compression.setter
    def last_compression(self, value: Dict[str, Any]) -> None:
        try:
            self._inner.last_compression = value
        except AttributeError:
            pass

    @property
    def last_context_tokens(self) -> int:
        return int(getattr(self._inner, "last_context_tokens", 0) or 0)

    @last_context_tokens.setter
    def last_context_tokens(self, value: int) -> None:
        try:
            self._inner.last_context_tokens = value
        except AttributeError:
            pass

    def get_model_name(self) -> str:
        name = getattr(self._inner, "get_model_name", None)
        return name() if name else getattr(self._inner, "model", "unknown")

    def clone(self, model: Optional[str] = None):
        return HistoryReplayProvider(self._inner.clone(model), self._history)

    async def chat_completion(self, messages, tools=None, **kwargs: Any):
        merged = self._inject(messages)
        response = await self._inner.chat_completion(merged, tools=tools, **kwargs)
        self.last_reasoning = getattr(self._inner, "last_reasoning", "") or ""
        return response

    def _inject(self, messages):
        if not self._history:
            return list(messages)
        from onion_harness.harness.bridge import EngineMessage

        system: list = []
        body: list = []
        for message in messages:
            if getattr(message, "type", None) == "system":
                system.append(message)
            else:
                body.append(message)

        replay = []
        for turn in self._history:
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            role = str(turn.get("role") or "user").lower()
            if role == "assistant":
                replay.append(EngineMessage.assistant(text=text))
            else:
                replay.append(EngineMessage.user(text=text))

        return system + replay + body

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()


class CompressingProvider(ReasoningCapture, EngineProviderBase):
    """Decorator that compresses old turns before delegating to a provider."""

    def __init__(self, inner, compressor: ContextCompressor) -> None:
        self._inner = inner
        self._compressor = compressor
        self.last_compression = {"compressed": False, "before": 0, "after": 0, "summary": ""}
        self.last_context_tokens = 0

    @property
    def inner(self):
        return self._inner

    def get_model_name(self) -> str:
        inner_name = getattr(self._inner, "get_model_name", None)
        return inner_name() if inner_name else getattr(self._inner, "model", "unknown")

    def clone(self, model: Optional[str] = None):
        return CompressingProvider(self._inner.clone(model), self._compressor)

    async def chat_completion(self, messages, tools=None, **kwargs: Any):
        compressed, stats = self._compressor.compress(messages)
        self.last_compression = stats
        self.last_context_tokens = sum(estimate_tokens(_message_text(m)) for m in compressed)
        try:
            response = await self._inner.chat_completion(compressed, tools=tools, **kwargs)
        except BoundaryError:
            raise
        except Exception as exc:  # noqa: BLE001
            friendly = friendly_provider_error(exc)
            raise BoundaryError(friendly.public_message, code=friendly.code, detail=friendly.detail) from None
        self.last_reasoning = getattr(self._inner, "last_reasoning", "") or ""
        return response

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()


def build_provider(
    provider_id: str,
    base_url: str,
    api_key: str,
    model: str,
    *,
    wire_format: str = "openai",
    temperature: Optional[float] = None,
    thinking_budget: int = 0,
    timeout: float = 180.0,
) -> Any:
    """Construct the raw (uncompressed) provider for a model configuration."""
    require_engine()
    if not base_url:
        raise BoundaryError("A base URL is required for the selected provider.", code="config")
    if not model:
        raise BoundaryError("Select a model before running a task.", code="config")

    if wire_format == "anthropic":
        return AnthropicProvider(
            base_url=base_url,
            api_key=api_key or "missing",
            model=model,
            timeout=timeout,
            temperature=temperature,
            thinking_budget=thinking_budget,
        )

    reasoning_effort = _reasoning_effort(provider_id, model, thinking_budget)
    return OnionOpenAIProvider(
        base_url=base_url,
        api_key=api_key or "missing",
        model=model,
        timeout=timeout,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )


def _reasoning_effort(provider_id: str, model: str, budget: int) -> Optional[str]:
    """Map the thinking dial onto OpenAI's ``reasoning_effort`` where it exists."""
    if provider_id != "openai" or budget <= 0:
        return None
    lowered = model.lower()
    if any(token in lowered for token in ("o1", "o3", "o4", "gpt-5")):
        if budget >= 8000:
            return "high"
        if budget >= 3000:
            return "medium"
        return "low"
    return None


def _append_block(
    converted: List[Dict[str, Any]], role: str, blocks: List[Dict[str, Any]]
) -> None:
    """Append, merging with the previous message when roles match."""
    if converted and converted[-1]["role"] == role:
        converted[-1]["content"].extend(blocks)
    else:
        converted.append({"role": role, "content": list(blocks)})


def _join_reasoning_details(details: Any) -> str:
    if not isinstance(details, list):
        return ""
    parts = []
    for item in details:
        if isinstance(item, dict):
            text = item.get("text") or item.get("summary") or ""
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def _message_text(message: Any) -> str:
    chunks = [getattr(message, "text", "") or ""]
    for call in getattr(message, "tool_calls", []) or []:
        chunks.append(json.dumps(getattr(call, "arguments", {}) or {}, ensure_ascii=False))
    for result in getattr(message, "tool_results", []) or []:
        chunks.append(getattr(result, "content", "") or "")
    return "\n".join(chunks)


__all__ = [
    "system_ssl_context",
    "http_client",
    "OnionOpenAIProvider",
    "AnthropicProvider",
    "CompressingProvider",
    "HistoryReplayProvider",
    "build_provider",
]