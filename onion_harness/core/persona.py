"""Personas - a named tone and sampling profile for the agent.

A persona is **prompt + sampling only**. It deliberately has no field that could
relax a safety rule: no mode, no elevation, no tool permissions. That is a
structural guarantee, not a convention. Plan mode still refuses writes and
Standard still asks for approval no matter which persona is active, because the
mode contract is composed *after* the persona overlay and the enforcement lives
in the mode layer.

Two things a persona may shape:

* **overlay** - extra instructions stacked at the same level as the core system
  prompt, so it survives context compression like any other system text;
* **sampling** - ``temperature`` and ``top_p``, which hot-switch on the next
  request without a restart.

Definition files may be dropped into ``<data_dir>/personas``. They must carry
both sampling values: a file without them is rejected rather than defaulted,
because a silently-invented temperature is exactly the kind of surprise a persona
system must not produce.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("onion.persona")

TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0
TOP_P_MIN = 0.0
TOP_P_MAX = 1.0


@dataclass(frozen=True)
class PersonaSpec:
    """One persona. Immutable so a shared instance cannot be edited mid-run."""

    id: str
    label: str
    description: str = ""
    overlay: str = ""
    temperature: float = 0.7
    top_p: float = 1.0
    builtin: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "temperature", clamp_temperature(self.temperature))
        object.__setattr__(self, "top_p", clamp_top_p(self.top_p))

    @property
    def icon(self) -> str:
        return "persona"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "overlay": self.overlay,
            "sampling": {"temperature": self.temperature, "top_p": self.top_p},
            "builtin": self.builtin,
        }

    def sampling_label(self) -> str:
        """Compact rendering for the sidebar chip, e.g. ``T0.35 / P0.9``."""
        return f"T{_trim(self.temperature)} / P{_trim(self.top_p)}"


def _trim(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def clamp_temperature(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(TEMPERATURE_MIN, min(TEMPERATURE_MAX, number))


def clamp_top_p(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(TOP_P_MIN, min(TOP_P_MAX, number))


BUILTIN_PERSONAS: Tuple[PersonaSpec, ...] = (
    PersonaSpec(
        id="default",
        label="Default",
        description="Balanced and careful. No extra shaping.",
        overlay="",
        temperature=0.7,
        top_p=1.0,
    ),
    PersonaSpec(
        id="deliberator",
        label="Deliberator",
        description="Thinks from several angles before acting; asks when unsure.",
        overlay=(
            "You deliberate before acting. Work the problem from more than one angle, "
            "state the trade-offs you considered, and when the situation is genuinely "
            "ambiguous ask the user instead of guessing. Read before you write."
        ),
        temperature=0.85,
        top_p=0.95,
    ),
    PersonaSpec(
        id="operator",
        label="Operator",
        description="Acts directly, then verifies with follow-up tool calls.",
        overlay=(
            "You act decisively. Prefer doing over asking when the intent is clear, then "
            "verify the result with a follow-up tool call before reporting. State what you "
            "changed and what you observed."
        ),
        temperature=0.3,
        top_p=0.9,
    ),
    PersonaSpec(
        id="auditor",
        label="Auditor",
        description="Sceptical and evidence-driven; cites what it saw.",
        overlay=(
            "You are sceptical by default. Treat unverified claims as unknown, cite the "
            "tool output behind every conclusion, and call out anything you could not "
            "confirm. Never round a partial result up to success."
        ),
        temperature=0.2,
        top_p=0.85,
    ),
    PersonaSpec(
        id="mentor",
        label="Mentor",
        description="Explains the reasoning as it works, assuming a learner.",
        overlay=(
            "You explain while you work. Briefly say why you are taking each step, name the "
            "concept involved, and point out the pitfall the user is most likely to hit. "
            "Keep it short; do not lecture."
        ),
        temperature=0.75,
        top_p=0.95,
    ),
)

BUILTIN_BY_ID: Dict[str, PersonaSpec] = {spec.id: spec for spec in BUILTIN_PERSONAS}
DEFAULT_PERSONA_ID = "default"


def persona_for(persona_id: Optional[str]) -> PersonaSpec:
    """Resolve an id to a persona, falling back to the default.

    An unknown id is a configuration drift (a removed custom persona), not an
    error worth failing a run over.
    """
    return BUILTIN_BY_ID.get((persona_id or "").strip().lower(), BUILTIN_BY_ID[DEFAULT_PERSONA_ID])


def all_personas() -> List[Dict[str, Any]]:
    return [spec.to_dict() for spec in BUILTIN_PERSONAS]


def compose_persona_overlay(persona: Optional[PersonaSpec], base: str) -> str:
    """Stack the persona's overlay on the core prompt.

    The caller must append the mode contract *after* this. Keeping that order in
    one place (the runtime's prompt composer) is what makes "persona can never
    override safety" structural rather than a promise.
    """
    if persona is None or not persona.overlay.strip():
        return base
    block = f"## Persona: {persona.label}\n{persona.overlay.strip()}"
    return f"{base}\n\n{block}".strip()


# ------------------------------------------------------------------ library
@dataclass
class PersonaLibrary:
    """Built-ins plus any valid definition files found on disk.

    Custom personas are ordinary JSON files under ``<data_dir>/personas``. Writing
    through this class (rather than editing the files by hand) means the same
    validation applies to a GUI edit as to a hand-written file, so a persona can
    never enter the system through a weaker path.
    """

    personas: List[PersonaSpec] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    directory: Optional[Path] = None

    def get(self, persona_id: Optional[str]) -> PersonaSpec:
        wanted = (persona_id or "").strip().lower()
        for spec in self.personas:
            if spec.id == wanted:
                return spec
        for spec in self.personas:
            if spec.id == DEFAULT_PERSONA_ID:
                return spec
        return BUILTIN_BY_ID[DEFAULT_PERSONA_ID]

    def to_public(self) -> List[Dict[str, Any]]:
        return [spec.to_dict() for spec in self.personas]

    # -- custom personas ----------------------------------------------------
    def save(
        self,
        *,
        persona_id: str,
        label: str,
        overlay: str,
        temperature: Any,
        top_p: Any,
        description: str = "",
    ) -> PersonaSpec:
        """Create or replace a custom persona. Raises :class:`PersonaError`."""
        if self.directory is None:
            raise PersonaError("personas cannot be saved: no data directory configured")

        clean_id = str(persona_id or "").strip().lower()
        requires = validate_persona_fields(
            persona_id=clean_id,
            label=label,
            overlay=overlay,
            temperature=temperature,
            top_p=top_p,
        )
        if requires:
            raise PersonaError(requires)

        spec = PersonaSpec(
            id=clean_id,
            label=str(label).strip(),
            description=str(description or "").strip(),
            overlay=str(overlay).strip(),
            temperature=clamp_temperature(temperature),
            top_p=clamp_top_p(top_p),
            builtin=False,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{clean_id}.json"
        payload = {
            "id": spec.id,
            "label": spec.label,
            "description": spec.description,
            "overlay": spec.overlay,
            "sampling": {"temperature": spec.temperature, "top_p": spec.top_p},
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

        # Update the in-memory list so the change applies without a restart.
        self.personas = [item for item in self.personas if item.id != spec.id] + [spec]
        self.personas.sort(key=lambda item: (not item.builtin, item.label.lower()))
        self.errors = [error for error in self.errors if not error.startswith(f"{path.name}:")]
        return spec

    def delete(self, persona_id: str) -> None:
        """Remove a custom persona. Built-ins are not deletable."""
        clean_id = str(persona_id or "").strip().lower()
        if clean_id in BUILTIN_BY_ID:
            raise PersonaError("built-in personas cannot be deleted")
        if self.directory is None:
            raise PersonaError("personas cannot be deleted: no data directory configured")

        path = self.directory / f"{clean_id}.json"
        if not path.exists():
            raise PersonaError(f"no custom persona named {clean_id!r}")
        path.unlink()
        self.personas = [item for item in self.personas if item.id != clean_id]

    @classmethod
    def load(cls, directory: Optional[str | Path] = None) -> "PersonaLibrary":
        personas: List[PersonaSpec] = list(BUILTIN_PERSONAS)
        errors: List[str] = []
        folder: Optional[Path] = Path(directory) if directory is not None else None
        if folder is not None and folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                spec, error = _load_persona_file(path)
                if error:
                    errors.append(error)
                    logger.warning("persona file ignored: %s", error)
                    continue
                assert spec is not None
                if spec.id in BUILTIN_BY_ID:
                    errors.append(
                        f"{path.name}: id {spec.id!r} collides with a built-in persona"
                    )
                    continue
                personas.append(spec)
        personas.sort(key=lambda item: (not item.builtin, item.label.lower()))
        return cls(personas=personas, errors=errors, directory=folder)


class PersonaError(ValueError):
    """A custom persona could not be saved. The message is shown to the user."""


def validate_persona_fields(
    *, persona_id: str, label: str, overlay: str, temperature: Any, top_p: Any
) -> str:
    """Return an error message, or an empty string when the values are usable."""
    clean_id = str(persona_id or "").strip().lower()
    if not clean_id:
        return "an id is required"
    if not all(ch.isalnum() or ch in "-_" for ch in clean_id):
        return "the id may contain only letters, digits, '-' and '_'"
    if len(clean_id) > 40:
        return "the id must be 40 characters or fewer"
    if clean_id in BUILTIN_BY_ID:
        return f"{clean_id!r} is a built-in persona; choose a different id"
    if not str(label or "").strip():
        return "a label is required"
    if not str(overlay or "").strip():
        # An empty overlay is the one thing that would make a persona do nothing
        # while appearing to be applied, which is worse than refusing the save.
        return "the persona prompt cannot be empty"
    if len(str(overlay)) > 8000:
        return "the persona prompt is too long (limit 8000 characters)"
    for name, value, low, high in (
        ("temperature", temperature, TEMPERATURE_MIN, TEMPERATURE_MAX),
        ("top_p", top_p, TOP_P_MIN, TOP_P_MAX),
    ):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{name} must be a number"
        if not (low <= number <= high):
            return f"{name} must be between {low:g} and {high:g}"
    return ""


def _load_persona_file(path: Path) -> Tuple[Optional[PersonaSpec], Optional[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name}: not valid JSON ({exc})"
    if not isinstance(raw, dict):
        return None, f"{path.name}: expected a JSON object"

    persona_id = str(raw.get("id") or path.stem).strip().lower()
    if not persona_id or not all(ch.isalnum() or ch in "-_" for ch in persona_id):
        return None, f"{path.name}: id must contain only letters, digits, '-' or '_'"

    label = str(raw.get("label") or persona_id).strip()
    overlay = str(raw.get("overlay") or "").strip()
    if not overlay:
        return None, f"{path.name}: overlay is empty"

    sampling = raw.get("sampling")
    if not isinstance(sampling, dict):
        return None, f"{path.name}: sampling is required and must include temperature and top_p"

    # Both values are mandatory on purpose: a persona silently inheriting a
    # temperature would make the sampling after a switch unpredictable.
    for key in ("temperature", "top_p"):
        if key not in sampling:
            return None, f"{path.name}: sampling.{key} is required"

    try:
        temperature = float(sampling["temperature"])
        top_p = float(sampling["top_p"])
    except (TypeError, ValueError):
        return None, f"{path.name}: sampling values must be numbers"

    if not (TEMPERATURE_MIN <= temperature <= TEMPERATURE_MAX):
        return None, f"{path.name}: temperature must be between 0 and 2"
    if not (TOP_P_MIN <= top_p <= TOP_P_MAX):
        return None, f"{path.name}: top_p must be between 0 and 1"

    return (
        PersonaSpec(
            id=persona_id,
            label=label,
            description=str(raw.get("description") or "").strip(),
            overlay=overlay,
            temperature=temperature,
            top_p=top_p,
            builtin=False,
        ),
        None,
    )


def write_persona_template(path: Path) -> None:
    """Write an example definition file the user can edit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "id": "example",
        "label": "Example",
        "description": "Copy this file and change the values.",
        "overlay": "Describe how this persona should behave, in plain language.",
        "sampling": {"temperature": 0.7, "top_p": 1.0},
    }
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "PersonaSpec",
    "PersonaLibrary",
    "BUILTIN_PERSONAS",
    "DEFAULT_PERSONA_ID",
    "persona_for",
    "all_personas",
    "compose_persona_overlay",
    "clamp_temperature",
    "clamp_top_p",
    "write_persona_template",
    "PersonaError",
    "validate_persona_fields",
]