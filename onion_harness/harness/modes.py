"""Execution modes - re-exported from the core layer.

The rules themselves live in :mod:`onion_harness.core.modes` so the plugin layer
can apply them without importing outward. This module keeps the harness-facing
import path stable.
"""

from __future__ import annotations

from onion_harness.core.modes import (  # noqa: F401
    ELEVATION_ASK,
    ELEVATION_ELEVATED,
    ELEVATION_LEVELS,
    ELEVATION_STANDARD,
    MODE_ORDER,
    MODES,
    PLAN,
    STANDARD,
    STRICT,
    STRICT_CONTRACT,
    ModeSpec,
    all_modes,
    compose_mode_prompt,
    looks_like_write,
    mode_spec,
    plan_mode_violation,
    requires_approval,
)

__all__ = [
    "PLAN",
    "STANDARD",
    "STRICT",
    "MODE_ORDER",
    "MODES",
    "ModeSpec",
    "mode_spec",
    "all_modes",
    "compose_mode_prompt",
    "plan_mode_violation",
    "requires_approval",
    "looks_like_write",
    "ELEVATION_LEVELS",
    "ELEVATION_STANDARD",
    "ELEVATION_ASK",
    "ELEVATION_ELEVATED",
    "STRICT_CONTRACT",
]
