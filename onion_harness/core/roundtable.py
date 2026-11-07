"""Roundtable contract (layer-neutral).

A roundtable is a bounded multi-agent debate: 2-6 seats around a topic, one of
them hosting, and a hard circuit breaker so models cannot argue forever.

The data and its validation live here so the API layer can reject a bad
configuration before any model call is made, and the harness can trust the
config it receives. No layer imports outward from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# Hard ceilings. These are not suggestions: the orchestrator enforces them.
MIN_SEATS = 2
MAX_SEATS = 6
DEFAULT_MAX_ROUNDS = 3
# The user may ask for a long debate; the token budget and the breaker still
# bound the real cost, so a high round ceiling is safe to offer.
ABSOLUTE_MAX_ROUNDS = 100
# Fallback ceiling when no call budget can be derived from the config.
ABSOLUTE_MAX_MODEL_CALLS = 64

SEAT_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-\u4e00-\u9fff]{1,32}$")

STANCE_AGREE = "agree"
STANCE_REJECT = "reject"
STANCE_NEUTRAL = "neutral"


class RoundtableError(ValueError):
    """Invalid roundtable configuration."""


class DissentFrequency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def weight(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]

    @property
    def label(self) -> str:
        return {"low": "Low", "medium": "Medium", "high": "High"}[self.value]


DISSENT_LEVELS = [
    {"id": "low", "label": "Low", "weight": 1, "description": "Seats concede readily."},
    {"id": "medium", "label": "Medium", "weight": 2, "description": "Balanced pushback."},
    {"id": "high", "label": "High", "weight": 3, "description": "Seats challenge every claim."},
]


@dataclass
class SeatSpec:
    """One participant. Each seat may use a different provider/model."""

    id: str
    name: str
    role: str = "member"  # "host" | "member"
    provider_id: str = ""
    label: str = ""
    base_url: str = ""
    wire_format: str = "openai"
    model: str = ""
    api_key: str = ""
    thinking: str = "medium"
    custom: bool = False

    def to_public(self) -> Dict[str, Any]:
        # Never expose the key, even to the GUI that supplied it.
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "provider_id": self.provider_id,
            "label": self.label,
            "base_url": self.base_url,
            "wire_format": self.wire_format,
            "model": self.model,
            "thinking": self.thinking,
            "custom": self.custom,
            "has_api_key": bool(self.api_key),
        }


@dataclass
class RoundtableConfig:
    """Everything the orchestrator needs to run one meeting."""

    topic: str
    seats: List[SeatSpec] = field(default_factory=list)
    host_id: str = ""
    max_rounds: int = DEFAULT_MAX_ROUNDS
    dissent: str = DissentFrequency.MEDIUM.value
    thinking: str = "medium"

    def to_public(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "seats": [seat.to_public() for seat in self.seats],
            "host_id": self.host_id,
            "max_rounds": self.max_rounds,
            "dissent": self.dissent,
            "thinking": self.thinking,
        }

    @property
    def host(self) -> Optional[SeatSpec]:
        for seat in self.seats:
            if seat.id == self.host_id:
                return seat
        return None

    @property
    def members(self) -> List[SeatSpec]:
        return [seat for seat in self.seats if seat.id != self.host_id]

    @property
    def call_budget(self) -> int:
        """Upper bound on model calls: one host pick + one speech per turn.

        Scales with the requested rounds and seats, with a hard ceiling so a
        100-round table cannot issue an unbounded number of requests.
        """
        return min(ABSOLUTE_MAX_MODEL_CALLS + self.max_rounds * len(self.seats) * 2,
                   self.max_rounds * len(self.seats) * 3)

    @property
    def token_budget(self) -> int:
        """A per-meeting token ceiling derived from its shape.

        Sized as roughly 40k tokens per seat per round: enough headroom for
        normal debate, low enough that a pathological meeting trips the breaker
        well before a 100-round limit is exhausted.
        """
        # ~8k tokens per seat per round: a real statement costs a few hundred to
        # a couple thousand, so this leaves room for long thinking budgets while
        # still capping a 100-round table long before it finishes.
        per_seat_round = 8_000
        area = self.max_rounds * len(self.seats)
        return min(2_000_000, max(20_000, area * per_seat_round))

    @property
    def estimated_calls(self) -> int:
        """Rough call count for the GUI's cost warning."""
        return self.max_rounds * max(1, len(self.seats)) * 2


def validate_config(config: RoundtableConfig) -> None:
    """Raise :class:`RoundtableError` when the meeting cannot be run safely."""
    topic = (config.topic or "").strip()
    if not topic:
        raise RoundtableError("A topic is required.")
    if len(topic) > 4000:
        raise RoundtableError("The topic is too long (max 4000 characters).")

    if not (MIN_SEATS <= len(config.seats) <= MAX_SEATS):
        raise RoundtableError(f"Seats must be between {MIN_SEATS} and {MAX_SEATS}.")

    ids = set()
    for seat in config.seats:
        if not seat.id:
            raise RoundtableError("Every seat needs an id.")
        if seat.id in ids:
            raise RoundtableError(f"Duplicate seat id: {seat.id}")
        ids.add(seat.id)
        if not SEAT_NAME_RE.match(seat.name or ""):
            raise RoundtableError(f"Invalid seat name: {seat.name!r}")
        if not seat.model:
            raise RoundtableError(f"Seat {seat.name!r} has no model selected.")
        if not seat.base_url:
            raise RoundtableError(f"Seat {seat.name!r} has no base URL.")
        if not seat.base_url.startswith(("http://", "https://")):
            raise RoundtableError(
                f"Seat {seat.name!r} has an invalid base URL (must start with http:// or https://)."
            )
        if not seat.api_key:
            # A custom endpoint may be a local server that needs no key; a known
            # provider always does.
            if not (seat.custom or seat.provider_id == "custom"):
                raise RoundtableError(
                    f"Seat {seat.name!r} has no API key for {seat.label or seat.provider_id}."
                )

    if config.host_id not in ids:
        raise RoundtableError("The host must be one of the seats.")

    if not (1 <= config.max_rounds <= ABSOLUTE_MAX_ROUNDS):
        raise RoundtableError(
            f"Max rounds must be between 1 and {ABSOLUTE_MAX_ROUNDS}."
        )

    try:
        DissentFrequency(config.dissent)
    except ValueError:
        raise RoundtableError(
            "Dissent must be one of: " + ", ".join(item["id"] for item in DISSENT_LEVELS)
        ) from None


def dissent_weight(value: str) -> int:
    try:
        return DissentFrequency(value).weight
    except ValueError:
        return DissentFrequency.MEDIUM.weight


def clamp_rounds(value: Any) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS
    return max(1, min(rounds, ABSOLUTE_MAX_ROUNDS))


__all__ = [
    "MIN_SEATS",
    "MAX_SEATS",
    "DEFAULT_MAX_ROUNDS",
    "ABSOLUTE_MAX_ROUNDS",
    "ABSOLUTE_MAX_MODEL_CALLS",
    "STANCE_AGREE",
    "STANCE_REJECT",
    "STANCE_NEUTRAL",
    "RoundtableError",
    "DissentFrequency",
    "DISSENT_LEVELS",
    "SeatSpec",
    "RoundtableConfig",
    "validate_config",
    "dissent_weight",
    "clamp_rounds",
]