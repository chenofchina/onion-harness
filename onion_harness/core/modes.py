"""Execution modes (layer-neutral) - Plan / Standard / Strict Coding.

Lives in core because both the plugin layer (which must refuse writes in Plan
mode) and the harness layer need the same rules, and core may not import
outward.

A mode shapes three things:

* **the system prompt** (Strict injects the anti-hallucination contract),
* **what the sandbox may do** (Plan is read-only), and
* **whether a tool call needs human approval** (Standard asks, Plan never runs
  writers without asking, Strict allows after verification prompts).

Modes are data here, so the GUI, the harness and the meta layer all read the
same definitions and nothing drifts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

PLAN = "plan"
STANDARD = "standard"
STRICT = "strict"

STRICT_CONTRACT = """## Strict coding contract (enforced)

1. Zero-trust assertions: before answering or writing code, verify the
   environment with a tool call. Never code on assumptions.
2. Fact provenance: every technical conclusion must cite the tool output it
   came from, in the form [Source: <tool name>].
3. Self-verification: after generating code or modifying files, run a
   verification command. If it fails, report the failure immediately and
   verbatim. Never claim success without evidence.
4. Mandatory preview: when starting a local service or changing frontend code,
   open the Preview panel so the user can see the result.

Violating any of these invalidates the response."""

PLAN_CONTRACT = """## Plan mode (read-only)

You are in planning mode. Inspect, read and reason freely, but do not write
files, install packages, change configuration, or run commands with system side
effects. Describe the plan you would execute and wait for the user to switch
modes before making changes."""


@dataclass(frozen=True)
class ModeSpec:
    id: str
    label: str
    icon: str
    description: str
    read_only: bool = False
    requires_approval: bool = False
    inject_prompt: str = ""
    verifies_tool_output: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "icon": self.icon,
            "description": self.description,
            "read_only": self.read_only,
            "requires_approval": self.requires_approval,
            "verifies_tool_output": self.verifies_tool_output,
        }


MODES: Dict[str, ModeSpec] = {
    PLAN: ModeSpec(
        id=PLAN,
        label="Plan",
        icon="plan",
        description="Read-only reasoning. No writes, no system-changing commands.",
        read_only=True,
        inject_prompt=PLAN_CONTRACT,
    ),
    STANDARD: ModeSpec(
        id=STANDARD,
        label="Standard",
        icon="standard",
        description="Default. Tool calls that change state need your confirmation.",
        requires_approval=True,
    ),
    STRICT: ModeSpec(
        id=STRICT,
        label="Strict Coding",
        icon="strict",
        description="Anti-hallucination contract: verify before asserting, cite sources, self-check.",
        inject_prompt=STRICT_CONTRACT,
        verifies_tool_output=True,
    ),
}

MODE_ORDER = [PLAN, STANDARD, STRICT]


def mode_spec(mode: Optional[str]) -> ModeSpec:
    return MODES.get((mode or STANDARD).lower(), MODES[STANDARD])


def all_modes() -> List[Dict[str, Any]]:
    return [MODES[name].to_dict() for name in MODE_ORDER]


# -- elevation ---------------------------------------------------------------
ELEVATION_STANDARD = "standard"
ELEVATION_ASK = "ask"
ELEVATION_ELEVATED = "elevated"

ELEVATION_LEVELS = [
    {
        "id": ELEVATION_STANDARD,
        "label": "Standard",
        "description": "Run as the current user. No UAC prompt.",
    },
    {
        "id": ELEVATION_ASK,
        "label": "Ask",
        "description": "Ask before running anything that would need elevation.",
    },
    {
        "id": ELEVATION_ELEVATED,
        "label": "Elevated",
        "description": "Run executor commands elevated through UAC.",
    },
]

# Commands that a Plan-mode run must never execute.
_WRITE_MARKERS = (
    "rm ",
    "rmdir",
    "del ",
    "move ",
    "mv ",
    "cp ",
    "copy ",
    "mkdir",
    "touch",
    "chmod",
    "chown",
    "pip install",
    "npm install",
    "git commit",
    "git push",
    ">",
    ">>",
    "write",
    "unlink",
    "rename",
    "remove-item",
    "set-content",
    "out-file",
    "new-item",
    "start-process",
    "shutdown",
    "reg add",
    "reg delete",
    "net user",
    "schtasks",
)


def looks_like_write(code: str) -> bool:
    """Heuristic: does this snippet appear to mutate state?"""
    lowered = (code or "").lower()
    return any(marker in lowered for marker in _WRITE_MARKERS)


def requires_approval(mode: Optional[str], code: str) -> bool:
    """Whether this call must wait for a human click before running.

    Standard mode asks only for state-changing calls; asking on every read would
    make the default experience unusable. Plan mode never runs writers at all
    (see :func:`plan_mode_violation`), and Strict Coding runs freely while
    demanding verification afterwards.
    """
    spec = mode_spec(mode)
    if not spec.requires_approval:
        return False
    return looks_like_write(code)


def plan_mode_violation(mode: Optional[str], code: str) -> Optional[str]:
    """Return a refusal message when Plan mode would be violated."""
    spec = mode_spec(mode)
    if not spec.read_only:
        return None
    if looks_like_write(code):
        return (
            "Blocked by Plan mode: this snippet appears to change state. "
            "Switch to Standard or Strict Coding to execute writes."
        )
    return None


def compose_mode_prompt(mode: Optional[str], base: str) -> str:
    spec = mode_spec(mode)
    if not spec.inject_prompt:
        return base
    return f"{base}\n\n{spec.inject_prompt}".strip()


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
    "looks_like_write",
    "ELEVATION_LEVELS",
    "ELEVATION_STANDARD",
    "ELEVATION_ASK",
    "ELEVATION_ELEVATED",
    "STRICT_CONTRACT",
]