"""RoundtableOrchestrator - bounded multi-agent debate.

Model: a host seat decides who speaks next; that seat answers and may rebut
specific peers; the loop repeats until consensus or the **hard circuit breaker**
trips.

Safety is structural, not advisory:

* ``max_rounds`` (default 3) is a hard stop. When it is reached without
  consensus the orchestrator emits a breaker event carrying every divergence and
  the fixed "human intervention required" message — it never keeps going.
* a token budget derived from the seat count caps the damage of a runaway
  meeting even inside the allowed rounds.
* every model call is counted against the budget derived from the config.

The orchestrator owns its own loop; it does not nest the ReAct loop.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from onion_harness.core.events import EventType, OnionEvent, event
from onion_harness.core.primitives import Layer
from onion_harness.core.roundtable import (
    STANCE_AGREE,
    STANCE_NEUTRAL,
    STANCE_REJECT,
    RoundtableConfig,
    SeatSpec,
    dissent_weight,
    validate_config,
)
from onion_harness.harness.bridge import EngineMessage, require_engine

logger = None  # set by the service if it wants roundtable logging

HUMAN_INTERVENTION = "当前无法达成共识，请人类介入。"

HOST_SYSTEM = """You are the host of a roundtable of independent AI experts.
Your job is to keep the discussion moving and decide who speaks next.

Reply with a single JSON object and nothing else:
{{"next": "<seat id>", "focus": "<one sentence on what they should address>", "close": false}}

- Pick the seat with the most to add about the open disagreement.
- Never pick the same seat twice in a row unless only one seat remains unheard.
- If the discussion has genuinely converged (everyone agrees and no open
  disagreement remains), set "close": true and leave "next" empty.

Seats:
{seats}

Transcript so far:
{transcript}
"""

SPEAKER_SYSTEM = """You are {name}, seat {index} of a roundtable of independent AI experts.
Address the topic directly and briefly (at most 120 words).

Reply with a single JSON object and nothing else:
{{"stance": "agree" | "reject" | "neutral",
  "content": "<your contribution>",
  "rebuts": ["<seat id>", ...],
  "reasoning": "<one sentence on why you take this stance>"}}

- "rebuts" lists the seat ids whose specific claim you are challenging. Leave it
  empty when you agree or add a new angle.
- Be concrete. No preamble, no restating the question.

Topic: {topic}
{dissent_hint}
Transcript so far:
{transcript}
"""


@dataclass
class _Turn:
    seat_id: str
    name: str
    round_no: int
    stance: str
    content: str
    reasoning: str
    rebuts: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    # Full chain of thought: the raw model reply plus the steps it implies.
    thought: List[Dict[str, str]] = field(default_factory=list)
    answered: str = ""
    raw: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "name": self.name,
            "round": self.round_no,
            "stance": self.stance,
            "content": self.content,
            "reasoning": self.reasoning,
            "rebuts": self.rebuts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_ms": self.elapsed_ms,
            "thought": self.thought,
            "answered": self.answered,
            "raw": self.raw[:2000],
        }


class RoundtableOrchestrator:
    """Runs one meeting and streams its events. Owns the loop, not the models."""

    def __init__(self, task_id: str, config: RoundtableConfig, api_keys: Optional[Dict[str, str]] = None) -> None:
        validate_config(config)
        self.task_id = task_id
        self.config = config
        self._api_keys = api_keys or {}
        self._seq = 0
        self._turns: List[_Turn] = []
        self._model_calls = 0
        self._total_tokens = 0
        # Budgets come from the config so a long meeting and a short one are
        # both bounded sensibly instead of sharing one fixed ceiling.
        self._token_budget = config.token_budget
        self._call_budget = config.call_budget
        self._breaker_tripped = False
        # Live interaction state. Human messages are addressed to all seats or
        # to one seat; both are folded into the transcript the seats read.
        self._human_messages: List[Dict[str, Any]] = []
        self._inbox: Dict[str, List[str]] = {}
        self._roundtable_done = False
        self._last_consensus: Optional[str] = None
        # Queued UI events (human interjections, seats joining) awaiting a drain.
        self._notices: List[Dict[str, Any]] = []

    # -- helpers ------------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, kind: EventType, **payload: Any) -> OnionEvent:
        return event(kind, self.task_id, self._next_seq(), source=Layer.HARNESS, **payload)

    def seat(self, seat_id: str) -> Optional[SeatSpec]:
        for seat in self.config.seats:
            if seat.id == seat_id:
                return seat
        return None

    @property
    def seat_index(self) -> Dict[str, int]:
        return {seat.id: index + 1 for index, seat in enumerate(self.config.seats)}

    # -- model calls --------------------------------------------------------
    async def _call_model(self, seat: SeatSpec, system: str, user: str) -> Tuple[str, int, int]:
        """One completion for one seat. Returns (text, input_tokens, output_tokens)."""
        require_engine()
        if self._model_calls >= self._call_budget:
            raise RuntimeError("model call ceiling reached")
        self._model_calls += 1

        from onion_harness.harness.presets import THINKING_BUDGET
        from onion_harness.harness.provider import CompressingProvider, build_provider
        from onion_harness.harness.context import ContextCompressor

        raw = build_provider(
            seat.provider_id,
            seat.base_url,
            seat.api_key,
            seat.model,
            wire_format=seat.wire_format,
            thinking_budget=THINKING_BUDGET.get(seat.thinking, 0),
        )
        provider = CompressingProvider(raw, ContextCompressor(keep_recent=6, max_tokens=48_000))
        messages = [
            EngineMessage.system(text=system),
            EngineMessage.user(text=user),
        ]
        response = await provider.chat_completion(messages)
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001
            pass

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self._total_tokens += input_tokens + output_tokens
        return (getattr(response, "text", "") or "").strip(), input_tokens, output_tokens

    # -- prompts ------------------------------------------------------------
    def _transcript(self, limit: int = 8) -> str:
        if not self._turns and not self._human_messages:
            return "(the meeting has just started; nobody has spoken yet)"
        lines = []
        for turn in self._turns[-limit:]:
            lines.append(
                f"[R{turn.round_no}] {turn.name} ({turn.stance}): {turn.content[:400]}"
            )
        # Human interjections belong in the shared context: seats must see that
        # the room was addressed, even before anyone has spoken.
        for message in self._human_messages[-4:]:
            scope = "" if message.get("to") in (None, "", "all") else f" -> {message['to']}"
            lines.append(f"[Human{scope}] {str(message.get('text', ''))[:400]}")
        return "\n".join(lines)

    def _seats_block(self) -> str:
        index = self.seat_index
        return "\n".join(
            f"- id={seat.id} name={seat.name} (seat {index[seat.id]})"
            for seat in self.config.seats
        )

    def _dissent_hint(self) -> str:
        weight = dissent_weight(self.config.dissent)
        if weight <= 1:
            return "Dissent frequency: low. Concede when you have no strong objection."
        if weight >= 3:
            return (
                "Dissent frequency: high. Actively challenge weak claims; silence is "
                "not agreement."
            )
        return "Dissent frequency: medium. Push back where you genuinely disagree."

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        """Extract the first JSON object from a model reply, tolerantly."""
        if not text:
            return {}
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                return {}
            candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # -- main loop ----------------------------------------------------------
    async def run(self) -> AsyncIterator[OnionEvent]:
        seats_public = [seat.to_public() for seat in self.config.seats]
        yield self._emit(
            EventType.ROUNDTABLE_START,
            topic=self.config.topic,
            seats=seats_public,
            host_id=self.config.host_id,
            max_rounds=self.config.max_rounds,
            dissent=self.config.dissent,
            token_budget=self._token_budget,
        )

        consensus: Optional[str] = None
        last_round = 0

        try:
            for round_no in range(1, self.config.max_rounds + 1):
                last_round = round_no
                yield self._emit(
                    EventType.ROUNDTABLE_ROUND,
                    round=round_no,
                    max_rounds=self.config.max_rounds,
                )

                # Flip any human interjections into the live stream before the
                # seats answer, so the hall and the transcript stay in step.
                for pending in self.drain_notices():
                    yield pending

                order = await self._speaking_order(round_no)
                for seat_id in order:
                    if self._budget_exhausted():
                        break

                    seat = self.seat(seat_id)
                    if seat is None:
                        continue

                    yield self._emit(
                        EventType.ROUNDTABLE_TURN,
                        round=round_no,
                        seat_id=seat.id,
                        name=seat.name,
                    )

                    turn = await self._speak(seat, round_no)
                    self._turns.append(turn)
                    yield self._emit(EventType.ROUNDTABLE_SPEECH, **turn.to_payload())

                    # Rebuttal edges are what the 2D view draws as red curves.
                    for target_id in turn.rebuts:
                        target = self.seat(target_id)
                        if target is None:
                            continue
                        yield self._emit(
                            EventType.ROUNDTABLE_REBUTTAL,
                            round=round_no,
                            from_seat=seat.id,
                            from_name=seat.name,
                            to_seat=target.id,
                            to_name=target.name,
                            reasoning=turn.reasoning,
                        )

                if self._budget_exhausted():
                    self._breaker_tripped = True
                    yield self._breaker(last_round, reason="token-budget")
                    break

                verdict = self._evaluate_round(round_no)
                yield self._emit(
                    EventType.ROUNDTABLE_CONSENSUS,
                    round=round_no,
                    reached=verdict["reached"],
                    summary=verdict["summary"],
                    agreements=verdict["agreements"],
                    divergences=verdict["divergences"],
                )
                if verdict["reached"]:
                    consensus = verdict["summary"]
                    self._last_consensus = consensus
                    break

                # Hard stop: after the final allowed round there is no next round.
                if round_no >= self.config.max_rounds:
                    self._breaker_tripped = True
                    yield self._breaker(round_no, reason="max-rounds", verdict=verdict)
                    break

        except Exception as exc:  # noqa: BLE001 - surface, never loop
            yield self._emit(
                EventType.ERROR,
                message=f"Roundtable stopped: {type(exc).__name__}: {exc}",
                code="roundtable",
            )
            self._roundtable_done = True
            yield self._emit(
                EventType.ROUNDTABLE_END,
                consensus=None,
                rounds=last_round,
                requires_human=True,
                message=HUMAN_INTERVENTION,
                reason="error",
            )
            return

        self._roundtable_done = True
        yield self._emit(
            EventType.ROUNDTABLE_END,
            consensus=consensus,
            rounds=last_round,
            requires_human=self._breaker_tripped,
            message="" if consensus else HUMAN_INTERVENTION,
            reason="breaker" if self._breaker_tripped else "consensus",
            total_tokens=self._total_tokens,
            model_calls=self._model_calls,
        )

    @property
    def finished(self) -> bool:
        return self._roundtable_done

    async def follow_up(
        self, text: str, to_seat: str = "all"
    ) -> AsyncIterator[OnionEvent]:
        """Answer a message sent after the meeting closed.

        The main loop has returned by then, so nothing would consume the inbox.
        This runs a single bounded round of replies and re-opens the floor for
        as many turns as the message warrants, without resuming the debate or
        resetting the recorded outcome.
        """
        # The message itself was already recorded (and its event published) by
        # the caller; recording it again here would duplicate the hall entry.
        if not self._human_messages or self._human_messages[-1].get("text") != text.strip():
            record = self.receive_human_message(text, to_seat)
            yield self._emit(EventType.ROUNDTABLE_MESSAGE, **record)

        targets = (
            [s for s in self.config.seats if s.id == to_seat]
            if to_seat not in ("", "all")
            else list(self.config.seats)
        )
        # A directed message gets one reply; a broadcast gets everyone once,
        # capped so a long table cannot be addressed into a second meeting.
        round_no = (self._turns[-1].round_no if self._turns else 0) + 1
        for seat in targets[: max(1, min(len(targets), 3 if to_seat in ("", "all") else 1))]:
            if self._budget_exhausted():
                yield self._emit(
                    EventType.ROUNDTABLE_NOTICE,
                    message=HUMAN_INTERVENTION,
                    reason="token-budget",
                )
                return

            turn = await self._speak(seat, round_no)
            self._turns.append(turn)
            yield self._emit(EventType.ROUNDTABLE_SPEECH, **turn.to_payload())

            for target_id in turn.rebuts:
                target = self.seat(target_id)
                if target is None:
                    continue
                yield self._emit(
                    EventType.ROUNDTABLE_REBUTTAL,
                    round=round_no,
                    from_seat=seat.id,
                    from_name=seat.name,
                    to_seat=target.id,
                    to_name=target.name,
                    reasoning=turn.reasoning,
                )

        yield self._emit(
            EventType.ROUNDTABLE_END,
            consensus=self._last_consensus,
            rounds=round_no,
            requires_human=False,
            message="",
            reason="follow-up",
            total_tokens=self._total_tokens,
            model_calls=self._model_calls,
        )

    # -- loop pieces --------------------------------------------------------
    async def _speaking_order(self, round_no: int) -> List[str]:
        """Ask the host who goes first; the rest follow in seat order.

        Deterministic tail ordering keeps the 2D choreography readable and
        bounds the number of host calls to one per round.
        """
        host = self.config.host
        if host is None:
            return [seat.id for seat in self.config.members]

        prompt = HOST_SYSTEM.format(
            seats=self._seats_block(), transcript=self._transcript()
        )
        try:
            text, _in, _out = await self._call_model(
                host, prompt, f"Topic: {self.config.topic}\n\nWho speaks next in round {round_no}?"
            )
            parsed = self._parse_json(text)
        except Exception:  # noqa: BLE001 - fall back to rotation
            parsed = {}

        first = str(parsed.get("next") or "").strip()
        if first not in {seat.id for seat in self.config.seats}:
            # Rotation fallback: everyone speaks, starting from this round's offset.
            order = [seat.id for seat in self.config.members]
            if not order:
                return [host.id]
            offset = (round_no - 1) % len(order)
            return order[offset:] + order[:offset]

        # Host speaks last so it can summarise.
        others = [seat.id for seat in self.config.members if seat.id != first]
        return [first] + others + ([host.id] if host.id != first else [])

    async def _speak(self, seat: SeatSpec, round_no: int, directive: str = "") -> _Turn:
        index = self.seat_index.get(seat.id, 0)
        pending = list(self._inbox.get(seat.id, []))
        system = SPEAKER_SYSTEM.format(
            name=seat.name,
            index=index,
            topic=self.config.topic,
            dissent_hint=self._dissent_hint(),
            transcript=self._transcript(),
        )
        started = time.perf_counter()

        ask = f"Round {round_no}. Continue the discussion of: {self.config.topic}"
        if pending:
            bullets = "\n".join(f"- {text}" for text in pending)
            ask += (
                "\n\nThe human addressed you directly:\n"
                f"{bullets}\nAnswer them explicitly in your content."
            )
            self._inbox[seat.id] = []
        if directive:
            ask += f"\n\nHuman directive: {directive}"
        try:
            text, input_tokens, output_tokens = await self._call_model(seat, system, ask)
        except Exception as exc:  # noqa: BLE001
            return _Turn(
                seat_id=seat.id,
                name=seat.name,
                round_no=round_no,
                stance=STANCE_NEUTRAL,
                content=f"(This seat could not respond: {type(exc).__name__})",
                reasoning="Model call failed.",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        parsed = self._parse_json(text)
        stance = str(parsed.get("stance") or STANCE_NEUTRAL).lower()
        if stance not in {STANCE_AGREE, STANCE_REJECT, STANCE_NEUTRAL}:
            stance = STANCE_NEUTRAL

        content = str(parsed.get("content") or text or "").strip()
        if not content:
            content = "(no contribution)"

        valid_ids = {other.id for other in self.config.seats}
        rebuts = [
            str(item)
            for item in (parsed.get("rebuts") or [])
            if isinstance(item, str) and item in valid_ids and item != seat.id
        ][:3]

        reasoning = str(parsed.get("reasoning") or "").strip()
        return _Turn(
            seat_id=seat.id,
            name=seat.name,
            round_no=round_no,
            stance=stance,
            content=content,
            reasoning=reasoning,
            rebuts=rebuts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            thought=self._build_thought(
                seat, round_no, stance, content, rebuts, reasoning, pending, directive, text
            ),
            answered=", ".join(pending) if pending else "",
            raw=text,
        )

    def _build_thought(
        self,
        seat: SeatSpec,
        round_no: int,
        stance: str,
        content: str,
        rebuts: List[str],
        reasoning: str,
        pending: List[str],
        directive: str,
        raw: str,
    ) -> List[Dict[str, str]]:
        """Assemble a readable chain of thought for the seat inspector.

        The steps are derived from what the seat actually received and returned,
        so the panel never invents a rationale the model did not express.
        """
        steps: List[Dict[str, str]] = []
        prior = len(self._turns)
        context_note = (
            "opening statement"
            if prior == 0
            else f"after {prior} prior statement{'s' if prior != 1 else ''}"
        )
        steps.append(
            {
                "kind": "read",
                "label": "Read the room",
                "text": (
                    f"Round {round_no}, {context_note}; dissent set to "
                    f"{self.config.dissent}."
                ),
            }
        )
        if pending:
            steps.append(
                {
                    "kind": "human",
                    "label": "Human addressed this seat",
                    "text": "\n".join(pending),
                }
            )
        if directive:
            steps.append({"kind": "human", "label": "Human directive", "text": directive})
        if reasoning:
            steps.append({"kind": "reason", "label": "Reasoning", "text": reasoning})
        if rebuts:
            names = [self.seat(rid).name if self.seat(rid) else rid for rid in rebuts]
            steps.append(
                {
                    "kind": "decide",
                    "label": f"Challenged {', '.join(names)}",
                    "text": f"Took the {stance} stance against {', '.join(names)}.",
                }
            )
        else:
            steps.append(
                {"kind": "decide", "label": f"Stance: {stance}", "text": content[:400]}
            )
        if raw and raw.strip() and raw.strip() != content:
            steps.append({"kind": "reply", "label": "Raw model reply", "text": raw[:1200]})
        return steps

    # -- live interaction ---------------------------------------------------
    def drain_notices(self) -> List[OnionEvent]:
        """Return queued live-interaction events and clear the queue."""
        pending = self._notices
        self._notices = []
        events: List[OnionEvent] = []
        for notice in pending:
            kind = notice.pop("type", "message")
            if kind == "seat":
                events.append(self._emit(EventType.ROUNDTABLE_SEAT_ADDED, **notice))
            else:
                events.append(self._emit(EventType.ROUNDTABLE_MESSAGE, **notice))
        return events


    def receive_human_message(self, text: str, to_seat: str = "all") -> Dict[str, Any]:
        """Inject a message from the human into the debate.

        Addressing one seat puts the message in that seat's inbox so it is
        answered on its next turn; addressing all folds it into the shared
        transcript immediately.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("Message is empty.")
        record = {
            "text": text[:4000],
            "to": to_seat or "all",
            "round": self._turns[-1].round_no if self._turns else 0,
            "at": time.time(),
        }
        self._human_messages.append(record)
        if record["to"] not in ("", "all"):
            self._inbox.setdefault(record["to"], []).append(record["text"])
        self._notices.append({"type": "message", **record})
        return record

    def add_seat(self, seat: SeatSpec) -> bool:
        """Add a debater mid-meeting. Bounded by the same 6-seat ceiling."""
        from onion_harness.core.roundtable import MAX_SEATS

        if len(self.config.seats) >= MAX_SEATS:
            return False
        if any(existing.id == seat.id for existing in self.config.seats):
            return False
        self.config.seats.append(seat)
        self._notices.append(
            {
                "type": "seat",
                "seat": seat.to_public(),
                "round": self._turns[-1].round_no if self._turns else 0,
            }
        )
        return True



    def _evaluate_round(self, round_no: int) -> Dict[str, Any]:
        """Consensus = every seat agreed this round and nothing is contested."""
        this_round = [turn for turn in self._turns if turn.round_no == round_no]
        if not this_round:
            return {"reached": False, "summary": "", "agreements": [], "divergences": []}

        rejections = [turn for turn in this_round if turn.stance == STANCE_REJECT]
        contested = {target for turn in this_round for target in turn.rebuts}
        agrees = [turn for turn in this_round if turn.stance == STANCE_AGREE]

        reached = not rejections and not contested and len(agrees) >= max(1, len(this_round) - 1)

        # Divergences are per-seat so the GUI can list who disagrees with what.
        divergences = []
        for turn in self._turns:
            if turn.stance != STANCE_REJECT and not turn.rebuts:
                continue
            divergences.append(
                {
                    "round": turn.round_no,
                    "seat_id": turn.seat_id,
                    "name": turn.name,
                    "stance": turn.stance,
                    "point": turn.content[:240],
                    "reasoning": turn.reasoning,
                    "rebuts": list(turn.rebuts),
                }
            )

        agreements = [
            {"round": turn.round_no, "seat_id": turn.seat_id, "name": turn.name, "point": turn.content[:240]}
            for turn in self._turns
            if turn.stance == STANCE_AGREE
        ]

        summary = ""
        if reached:
            summary = agrees[0].content[:400] if agrees else "All seats converged."

        return {
            "reached": reached,
            "summary": summary,
            "agreements": agreements,
            "divergences": divergences,
        }

    def _breaker(self, round_no: int, reason: str, verdict: Optional[Dict[str, Any]] = None) -> OnionEvent:
        verdict = verdict or self._evaluate_round(round_no)
        return self._emit(
            EventType.ROUNDTABLE_BREAKER,
            round=round_no,
            reason=reason,
            message=HUMAN_INTERVENTION,
            requires_human=True,
            divergences=verdict.get("divergences", []),
            agreements=verdict.get("agreements", []),
            total_tokens=self._total_tokens,
            model_calls=self._model_calls,
            max_rounds=self.config.max_rounds,
        )

    def _budget_exhausted(self) -> bool:
        return self._total_tokens >= self._token_budget


__all__ = ["RoundtableOrchestrator", "HUMAN_INTERVENTION"]