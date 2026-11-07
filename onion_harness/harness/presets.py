"""Model provider presets and construction.

Presets are pure data: label, mono icon name, default base URL and a starter
model list.  Selecting a preset in the GUI prefills the base URL so the user
only supplies an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ModelPreset:
    id: str
    label: str
    base_url: str
    format: str  # "openai" | "anthropic"
    icon: str
    models: List[str] = field(default_factory=list)
    api_key_url: str = ""
    builtin: bool = True
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "format": self.format,
            "icon": self.icon,
            "models": list(self.models),
            "api_key_url": self.api_key_url,
            "builtin": self.builtin,
            "notes": self.notes,
        }


PRESETS: List[ModelPreset] = [
    ModelPreset(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        format="openai",
        icon="deepseek",
        models=["deepseek-chat", "deepseek-reasoner"],
        api_key_url="https://platform.deepseek.com/api_keys",
        notes="OpenAI-compatible. deepseek-reasoner emits reasoning content.",
    ),
    ModelPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        format="openai",
        icon="openai",
        models=["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini"],
        api_key_url="https://platform.openai.com/api-keys",
    ),
    ModelPreset(
        id="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        format="anthropic",
        icon="anthropic",
        models=[
            "claude-sonnet-4-5",
            "claude-opus-4-1",
            "claude-3-7-sonnet-latest",
            "claude-3-5-haiku-latest",
        ],
        api_key_url="https://console.anthropic.com/settings/keys",
        notes="Messages API, translated to the OpenAI compatibility layer.",
    ),
    ModelPreset(
        id="moonshot",
        label="Moonshot",
        base_url="https://api.moonshot.cn/v1",
        format="openai",
        icon="moonshot",
        models=["kimi-k2-0905-preview", "moonshot-v1-128k", "moonshot-v1-32k"],
        api_key_url="https://platform.moonshot.cn/console/api-keys",
    ),
    ModelPreset(
        id="zhipu",
        label="Zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        format="openai",
        icon="zhipu",
        models=["glm-4.6", "glm-4.5", "glm-4.5-air"],
        api_key_url="https://open.bigmodel.cn/usercenter/apikeys",
    ),
    ModelPreset(
        id="qwen",
        label="Qwen (DashScope)",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        format="openai",
        icon="qwen",
        models=["qwen3-max", "qwen3-coder-plus", "qwen-plus"],
        api_key_url="https://bailian.console.aliyun.com/",
    ),
    ModelPreset(
        id="siliconflow",
        label="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        format="openai",
        icon="siliconcloud",
        models=["deepseek-ai/DeepSeek-V3.2-Exp", "Qwen/Qwen3-Coder-480B-A35B-Instruct"],
        api_key_url="https://cloud.siliconflow.cn/account/ak",
    ),
    ModelPreset(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        format="openai",
        icon="openrouter",
        models=["anthropic/claude-sonnet-4.5", "openai/gpt-4.1", "deepseek/deepseek-chat"],
        api_key_url="https://openrouter.ai/keys",
    ),
    ModelPreset(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        format="openai",
        icon="gemini",
        models=["gemini-2.5-pro", "gemini-2.5-flash"],
        api_key_url="https://aistudio.google.com/apikey",
    ),
    ModelPreset(
        id="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        format="openai",
        icon="ollama",
        models=["qwen3:8b", "llama3.2", "deepseek-r1:8b"],
        notes="Runs locally; the API key can be any non-empty string.",
    ),
    ModelPreset(
        id="custom",
        label="Custom endpoint",
        base_url="",
        format="openai",
        icon="openai",
        models=[],
        notes="Any OpenAI-compatible or Anthropic-compatible base URL.",
    ),
]

PRESET_BY_ID: Dict[str, ModelPreset] = {p.id: p for p in PRESETS}


def preset_for(provider_id: str) -> Optional[ModelPreset]:
    return PRESET_BY_ID.get(provider_id)


def all_presets(extra: Optional[List[dict]] = None) -> List[dict]:
    presets = [p.to_dict() for p in PRESETS]
    for raw in extra or []:
        entry = {
            "id": raw.get("id") or raw.get("label", "custom").lower().replace(" ", "-"),
            "label": raw.get("label", "Custom"),
            "base_url": raw.get("base_url", ""),
            "format": raw.get("format", "openai"),
            "icon": raw.get("icon", "openai"),
            "models": list(raw.get("models") or []),
            "api_key_url": raw.get("api_key_url", ""),
            "builtin": False,
            "notes": raw.get("notes", ""),
        }
        if not any(p["id"] == entry["id"] for p in presets):
            presets.append(entry)
    return presets


THINKING_LEVELS = [
    {"id": "off", "label": "Off", "budget_tokens": 0},
    {"id": "low", "label": "Low", "budget_tokens": 1024},
    {"id": "medium", "label": "Medium", "budget_tokens": 4096},
    {"id": "high", "label": "High", "budget_tokens": 12000},
    {"id": "max", "label": "Max", "budget_tokens": 24000},
]

THINKING_BUDGET = {item["id"]: item["budget_tokens"] for item in THINKING_LEVELS}

# The thinking dial as a smooth slider: 0..4 steps map onto the levels above.
THINKING_SLIDER = {
    "min": 0,
    "max": len(THINKING_LEVELS) - 1,
    "step": 1,
    "stops": [item["id"] for item in THINKING_LEVELS],
    "labels": [item["label"] for item in THINKING_LEVELS],
}


def thinking_from_step(step: int) -> str:
    """Map a slider position onto a level id, clamped."""
    index = max(0, min(int(step), len(THINKING_LEVELS) - 1))
    return THINKING_LEVELS[index]["id"]


def step_from_thinking(level: str) -> int:
    for index, item in enumerate(THINKING_LEVELS):
        if item["id"] == level:
            return index
    return 0


__all__ = [
    "ModelPreset",
    "PRESETS",
    "PRESET_BY_ID",
    "preset_for",
    "all_presets",
    "THINKING_LEVELS",
    "THINKING_BUDGET",
    "THINKING_SLIDER",
    "thinking_from_step",
    "step_from_thinking",
]