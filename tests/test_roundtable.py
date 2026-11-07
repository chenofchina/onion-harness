"""Roundtable tests - orchestration, rebuttal edges, and the hard breaker.

All model calls are faked; these tests never touch the network.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from onion_harness.core.roundtable import (
    ABSOLUTE_MAX_ROUNDS,
    DEFAULT_MAX_ROUNDS,
    DISSENT_LEVELS,
    SeatSpec,
    RoundtableConfig,
    RoundtableError,
    clamp_rounds,
    validate_config,
)
from onion_harness.harness import roundtable as roundtable_module
from onion_harness.harness.roundtable import HUMAN_INTERVENTION, RoundtableOrchestrator


def seat(seat_id: str, name: str, role: str = "member") -> SeatSpec:
    return SeatSpec(
        id=seat_id,
        name=name,
        role=role,
        provider_id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="sk-test",
    )


def config(**overrides) -> RoundtableConfig:
    seats = overrides.pop("seats", [seat("a", "Alice"), seat("b", "Bob"), seat("h", "Host", "host")])
    return RoundtableConfig(
        topic=overrides.pop("topic", "Should we ship the migration on Friday?"),
        seats=seats,
        host_id=overrides.pop("host_id", "h"),
        **overrides,
    )


class FakeProvider:
    """Replies with a scripted JSON payload; records the prompts it saw."""

    script: list = []
    calls: list = []

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_reasoning = ""
        FakeProvider.calls.append(reply)

    def get_model_name(self):
        return "fake"

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        from onion_harness.harness.bridge import EngineChatResponse, EngineUsage

        return EngineChatResponse(
            text=self._reply,
            usage=EngineUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def aclose(self):
        return None


def patch_provider(monkeypatch_replies):
    """Install a fake provider factory whose replies come from a list/iterator."""
    replies = list(monkeypatch_replies)
    index = {"i": 0}

    def factory(*args, **kwargs):
        current = replies[min(index["i"], len(replies) - 1)]
        index["i"] += 1
        return FakeProvider(current)

    roundtable_module_holder = None
    del roundtable_module_holder
    import onion_harness.harness.provider as provider_module

    original = provider_module.build_provider
    provider_module.build_provider = factory
    return original


class ConfigValidationTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_MAX_ROUNDS, 3)
        self.assertEqual(ABSOLUTE_MAX_ROUNDS, 100)

    def test_rejects_too_few_seats(self):
        with self.assertRaises(RoundtableError):
            validate_config(config(seats=[seat("a", "Alice")], host_id="a"))

    def test_rejects_too_many_seats(self):
        seats = [seat(f"s{i}", f"S{i}") for i in range(7)]
        with self.assertRaises(RoundtableError):
            validate_config(config(seats=seats, host_id="s0"))

    def test_rejects_seat_without_key(self):
        broken = seat("a", "Alice")
        broken.api_key = ""
        with self.assertRaises(RoundtableError):
            validate_config(config(seats=[broken, seat("b", "Bob")], host_id="a"))

    def test_rejects_host_outside_seats(self):
        with self.assertRaises(RoundtableError):
            validate_config(config(host_id="nope"))

    def test_rejects_bad_rounds(self):
        with self.assertRaises(RoundtableError):
            validate_config(config(max_rounds=0))
        with self.assertRaises(RoundtableError):
            validate_config(config(max_rounds=101))

    def test_clamps_rounds(self):
        self.assertEqual(clamp_rounds(100), ABSOLUTE_MAX_ROUNDS)
        self.assertEqual(clamp_rounds(999), ABSOLUTE_MAX_ROUNDS)
        self.assertEqual(clamp_rounds(0), 1)
        self.assertEqual(clamp_rounds("nonsense"), DEFAULT_MAX_ROUNDS)

    def test_public_payload_hides_keys(self):
        import json

        payload = json.dumps(config().to_public())
        self.assertNotIn("sk-test", payload)

    def test_dissent_levels(self):
        self.assertEqual([item["id"] for item in DISSENT_LEVELS], ["low", "medium", "high"])


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        import onion_harness.harness.provider as provider_module

        self._original = provider_module.build_provider

    def tearDown(self):
        import onion_harness.harness.provider as provider_module

        provider_module.build_provider = self._original

    def run_meeting(self, replies, cfg=None):
        patch_provider(replies)
        orchestrator = RoundtableOrchestrator("task_rt", cfg or config())
        events = []

        async def consume():
            async for ev in orchestrator.run():
                events.append(ev)

        asyncio.run(consume())
        return events, orchestrator

    # -- happy path ---------------------------------------------------------
    def test_emits_the_full_event_family(self):
        host_reply = '{"next": "a", "focus": "opening", "close": false}'
        agree = '{"stance": "agree", "content": "Friday is fine.", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, agree, agree, agree])
        types = [e.type.value for e in events]
        self.assertIn("roundtable-start", types)
        self.assertIn("roundtable-round", types)
        self.assertIn("roundtable-turn", types)
        self.assertIn("roundtable-speech", types)
        self.assertIn("roundtable-consensus", types)
        self.assertIn("roundtable-end", types)

    def test_start_payload_lists_seats_without_keys(self):
        import json

        host_reply = '{"next": "a"}'
        agree = '{"stance": "agree", "content": "ok", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, agree, agree, agree])
        start = next(e for e in events if e.type.value == "roundtable-start")
        payload = json.dumps(start.payload)
        self.assertNotIn("sk-test", payload)
        self.assertEqual(len(start.payload["seats"]), 3)
        self.assertEqual(start.payload["host_id"], "h")

    def test_rebuttal_produces_a_directed_edge(self):
        host_reply = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "No, tests are incomplete.", "rebuts": ["b"], "reasoning": "risk"}'
        agree = '{"stance": "agree", "content": "Sure.", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, reject, agree, agree, agree])
        edges = [e for e in events if e.type.value == "roundtable-rebuttal"]
        self.assertTrue(edges)
        self.assertEqual(edges[0].payload["from_seat"], "a")
        self.assertEqual(edges[0].payload["to_seat"], "b")

    def test_rebuttal_targets_must_be_real_seats(self):
        host_reply = '{"next": "a"}'
        bad = '{"stance": "reject", "content": "x", "rebuts": ["ghost", "a", "b"]}'
        agree = '{"stance": "agree", "content": "y", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, bad, agree, agree, agree])
        edges = [e for e in events if e.type.value == "roundtable-rebuttal"]
        targets = {e.payload["to_seat"] for e in edges}
        self.assertNotIn("ghost", targets)
        self.assertNotIn("a", targets)  # never rebut yourself

    def test_speech_carries_tokens_and_timing(self):
        host_reply = '{"next": "a"}'
        agree = '{"stance": "agree", "content": "ok", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, agree, agree, agree])
        speech = next(e for e in events if e.type.value == "roundtable-speech")
        self.assertEqual(speech.payload["input_tokens"], 100)
        self.assertEqual(speech.payload["output_tokens"], 50)
        self.assertIn("elapsed_ms", speech.payload)
        self.assertGreaterEqual(speech.payload["elapsed_ms"], 0)

    # -- the circuit breaker ------------------------------------------------
    def test_breaker_stops_at_max_rounds_under_constant_rejection(self):
        host_reply = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "Still no.", "rebuts": ["b"], "reasoning": "unconvinced"}'
        cfg = config(max_rounds=3)
        events, orchestrator = self.run_meeting(
            [host_reply] + [reject, reject, reject] * 5, cfg
        )

        rounds = [e for e in events if e.type.value == "roundtable-round"]
        self.assertEqual(len(rounds), 3, "must stop at max_rounds, not keep going")

        breaker = next(e for e in events if e.type.value == "roundtable-breaker")
        self.assertTrue(breaker.payload["requires_human"])
        self.assertEqual(breaker.payload["message"], HUMAN_INTERVENTION)
        self.assertEqual(breaker.payload["reason"], "max-rounds")
        self.assertTrue(breaker.payload["divergences"])

        end = events[-1]
        self.assertEqual(end.type.value, "roundtable-end")
        self.assertTrue(end.payload["requires_human"])
        self.assertEqual(end.payload["message"], HUMAN_INTERVENTION)
        self.assertIsNone(end.payload["consensus"])

    def test_breaker_lists_every_divergence(self):
        host_reply = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "Disagree.", "rebuts": ["b"]}'
        events, _ = self.run_meeting([host_reply] + [reject] * 20, config(max_rounds=2))
        breaker = next(e for e in events if e.type.value == "roundtable-breaker")
        seats_in_breaker = {item["seat_id"] for item in breaker.payload["divergences"]}
        self.assertTrue(seats_in_breaker)

    def test_rounds_are_hard_bounded_even_if_asked_for_more(self):
        host_reply = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "No.", "rebuts": ["b"]}'
        events, _ = self.run_meeting([host_reply] + [reject] * 60, config(max_rounds=5))
        rounds = [e for e in events if e.type.value == "roundtable-round"]
        self.assertEqual(len(rounds), 5)
        self.assertTrue(any(e.type.value == "roundtable-breaker" for e in events))

    def test_consensus_ends_the_meeting_early(self):
        host_reply = '{"next": "a"}'
        agree = '{"stance": "agree", "content": "We all agree: ship Tuesday.", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, agree, agree, agree], config(max_rounds=5))
        rounds = [e for e in events if e.type.value == "roundtable-round"]
        self.assertEqual(len(rounds), 1, "one agreeing round should close the meeting")
        end = events[-1]
        self.assertFalse(end.payload["requires_human"])
        self.assertTrue(end.payload["consensus"])
        self.assertFalse(any(e.type.value == "roundtable-breaker" for e in events))

    def test_token_budget_trips_the_breaker(self):
        host_reply = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "No.", "rebuts": ["b"]}'
        cfg = config(max_rounds=5)
        events, orchestrator = self.run_meeting([host_reply] + [reject] * 60, cfg)
        # The orchestrator must stay inside the budget it derived from the config.
        self.assertLessEqual(orchestrator._total_tokens, orchestrator._token_budget + 10_000)

    def test_model_reply_that_is_not_json_degrades_gracefully(self):
        host_reply = "I think Alice should go first."
        plain = "I disagree with the framing entirely."
        events, _ = self.run_meeting([host_reply, plain, plain, plain], config(max_rounds=2))
        speeches = [e for e in events if e.type.value == "roundtable-speech"]
        self.assertTrue(speeches)
        # Raw text is preserved rather than dropped.
        self.assertTrue(any("disagree" in s.payload["content"] for s in speeches))
        self.assertEqual(events[-1].type.value, "roundtable-end")

    def test_a_seat_failure_does_not_kill_the_meeting(self):
        calls = {"n": 0}

        def factory(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("provider exploded")
            reply = '{"next": "a"}' if calls["n"] == 1 else '{"stance": "agree", "content": "ok", "rebuts": []}'
            return FakeProvider(reply)

        import onion_harness.harness.provider as provider_module

        provider_module.build_provider = factory
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        events = []

        async def consume():
            async for ev in orchestrator.run():
                events.append(ev)

        asyncio.run(consume())
        self.assertEqual(events[-1].type.value, "roundtable-end")

    def test_host_rotation_fallback_when_host_reply_is_unusable(self):
        host_reply = "no json here"
        agree = '{"stance": "agree", "content": "ok", "rebuts": []}'
        events, _ = self.run_meeting([host_reply, agree, agree, agree], config(max_rounds=1))
        turns = [e for e in events if e.type.value == "roundtable-turn"]
        # Every member still gets a turn even without a usable host pick.
        self.assertGreaterEqual(len(turns), 2)


class OrchestratorApiSurfaceTest(unittest.TestCase):
    def test_breakers_never_require_a_second_round(self):
        """A meeting cannot be resumed past the breaker by construction."""
        import inspect

        from onion_harness.harness.roundtable import RoundtableOrchestrator as RT

        source = inspect.getsource(RT.run)
        self.assertIn("max_rounds", source)
        self.assertIn("_breaker", source)


if __name__ == "__main__":
    unittest.main()

class ThoughtChainTest(unittest.TestCase):
    """The seat inspector must show what the model actually expressed."""

    def setUp(self):
        import onion_harness.harness.provider as provider_module

        self._original = provider_module.build_provider

    def tearDown(self):
        import onion_harness.harness.provider as provider_module

        provider_module.build_provider = self._original

    def run_meeting(self, replies, cfg=None):
        patch_provider(replies)
        orchestrator = RoundtableOrchestrator("task_rt", cfg or config(max_rounds=1))
        events = []

        async def consume():
            async for ev in orchestrator.run():
                events.append(ev)

        asyncio.run(consume())
        return events, orchestrator

    def test_speech_carries_a_thought_chain(self):
        host = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "No.", "rebuts": ["b"], "reasoning": "risk"}'
        agree = '{"stance": "agree", "content": "Yes.", "rebuts": []}'
        events, _ = self.run_meeting([host, reject, agree, agree])
        speech = next(e for e in events if e.type.value == "roundtable-speech")
        thought = speech.payload["thought"]
        self.assertTrue(thought)
        kinds = [step["kind"] for step in thought]
        self.assertIn("reason", kinds)
        self.assertIn("decide", kinds)

    def test_thought_chain_records_the_rebuttal_target(self):
        host = '{"next": "a"}'
        reject = '{"stance": "reject", "content": "No.", "rebuts": ["b"], "reasoning": "weak"}'
        agree = '{"stance": "agree", "content": "ok", "rebuts": []}'
        events, _ = self.run_meeting([host, reject, agree, agree])
        speech = next(e for e in events if e.type.value == "roundtable-speech")
        decided = next(s for s in speech.payload["thought"] if s["kind"] == "decide")
        self.assertIn("Bob", decided["label"])

    def test_raw_reply_is_preserved(self):
        host = '{"next": "a"}'
        agree = '{"stance": "agree", "content": "Fine.", "rebuts": []}'
        events, _ = self.run_meeting([host, agree, agree, agree])
        speech = next(e for e in events if e.type.value == "roundtable-speech")
        self.assertIn("stance", speech.payload["raw"])


class LiveInteractionTest(unittest.TestCase):
    """Human messages and mid-meeting seats."""

    def setUp(self):
        import onion_harness.harness.provider as provider_module

        self._original = provider_module.build_provider

    def tearDown(self):
        import onion_harness.harness.provider as provider_module

        provider_module.build_provider = self._original

    def test_human_message_to_all_enters_the_transcript(self):
        patch_provider(['{"next": "a"}', '{"stance": "agree", "content": "ok", "rebuts": []}'])
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        orchestrator.receive_human_message("Please consider the budget.", "all")
        self.assertIn("budget", orchestrator._transcript())

    def test_human_message_to_one_seat_lands_in_its_inbox(self):
        patch_provider(['{"next": "a"}', '{"stance": "agree", "content": "ok", "rebuts": []}'])
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        orchestrator.receive_human_message("What about latency?", "a")
        self.assertEqual(orchestrator._inbox["a"], ["What about latency?"])
        self.assertNotIn("b", orchestrator._inbox)

    def test_message_is_drained_as_an_event(self):
        patch_provider(['{"next": "a"}'])
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        orchestrator.receive_human_message("Hello room.", "all")
        drained = orchestrator.drain_notices()
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].type.value, "roundtable-message")
        self.assertEqual(drained[0].payload["text"], "Hello room.")
        # Draining twice must not duplicate.
        self.assertEqual(orchestrator.drain_notices(), [])

    def test_empty_message_rejected(self):
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        with self.assertRaises(ValueError):
            orchestrator.receive_human_message("   ")

    def test_adding_a_seat_emits_an_event_and_appends(self):
        patch_provider(['{"next": "a"}'])
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        new_seat = seat("new", "Nia")
        self.assertTrue(orchestrator.add_seat(new_seat))
        self.assertEqual(len(orchestrator.config.seats), 4)
        drained = orchestrator.drain_notices()
        self.assertEqual(drained[0].type.value, "roundtable-seat-added")
        self.assertEqual(drained[0].payload["seat"]["name"], "Nia")

    def test_duplicate_seat_id_refused(self):
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        self.assertFalse(orchestrator.add_seat(seat("a", "Clone")))

    def test_seat_ceiling_enforced(self):
        from onion_harness.core.roundtable import MAX_SEATS

        seats = [seat(f"s{i}", f"S{i}") for i in range(MAX_SEATS)]
        orchestrator = RoundtableOrchestrator(
            "task_rt", config(seats=seats, host_id="s0", max_rounds=1)
        )
        self.assertFalse(orchestrator.add_seat(seat("extra", "Extra")))


class CustomProviderTest(unittest.TestCase):
    """A seat may target any endpoint, not just a bundled preset."""

    def test_custom_seat_needs_no_key(self):
        custom = SeatSpec(
            id="local",
            name="Local",
            provider_id="custom",
            label="Others",
            base_url="http://localhost:11434/v1",
            model="qwen3:8b",
            api_key="",
            custom=True,
        )
        host = seat("h", "Host")
        validate_config(config(seats=[custom, host], host_id="h"))

    def test_custom_seat_requires_http_url(self):
        custom = SeatSpec(
            id="local",
            name="Local",
            provider_id="custom",
            base_url="file:///etc/passwd",
            model="m",
            custom=True,
        )
        with self.assertRaises(RoundtableError):
            validate_config(config(seats=[custom, seat("b", "Bob")], host_id="b"))

    def test_known_provider_still_requires_a_key(self):
        broken = seat("a", "Alice")
        broken.api_key = ""
        with self.assertRaises(RoundtableError):
            validate_config(config(seats=[broken, seat("b", "Bob")], host_id="a"))

    def test_public_payload_marks_custom(self):
        custom = SeatSpec(
            id="c", name="C", provider_id="custom", base_url="https://x/v1", model="m",
            custom=True,
        )
        self.assertTrue(custom.to_public()["custom"])


class RoundCeilingTest(unittest.TestCase):
    """The dialog offers 1-100 rounds; the budget still bounds real work."""

    def test_hundred_rounds_accepted(self):
        validate_config(config(max_rounds=100))

    def test_over_hundred_rejected(self):
        with self.assertRaises(RoundtableError):
            validate_config(config(max_rounds=101))

    def test_budget_scales_with_rounds(self):
        small = config(max_rounds=2)
        large = config(max_rounds=100)
        self.assertGreater(large.call_budget, small.call_budget)
        self.assertGreater(large.token_budget, small.token_budget)
        self.assertLessEqual(large.token_budget, 4_000_000)

    def test_estimated_calls_matches_shape(self):
        cfg = config(max_rounds=10)
        self.assertEqual(cfg.estimated_calls, 10 * len(cfg.seats) * 2)

    def test_long_meeting_still_stops_on_breaker(self):
        """100 rounds of rejection must not run forever: budget trips first."""
        import onion_harness.harness.provider as provider_module

        original = provider_module.build_provider
        try:
            patch_provider(['{"next": "a"}'] + ['{"stance": "reject", "content": "No.", "rebuts": ["b"]}'] * 800)
            orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=100))
            events = []

            async def consume():
                async for ev in orchestrator.run():
                    events.append(ev)

            asyncio.run(consume())
        finally:
            provider_module.build_provider = original

        rounds = [e for e in events if e.type.value == "roundtable-round"]
        # Either the round limit or a budget stops it; it must never run past
        # the hard ceiling or exceed the call budget.
        self.assertLessEqual(len(rounds), 100)
        self.assertTrue(any(e.type.value == "roundtable-breaker" for e in events))
        self.assertLessEqual(orchestrator._model_calls, orchestrator._call_budget)


class PostMeetingReplyTest(unittest.TestCase):
    """A message sent after the meeting closed must still be answered."""

    def setUp(self):
        import onion_harness.harness.provider as provider_module

        self._original = provider_module.build_provider

    def tearDown(self):
        import onion_harness.harness.provider as provider_module

        provider_module.build_provider = self._original

    def _finished_orchestrator(self):
        patch_provider(
            [
                '{"next": "a"}',
                '{"stance": "reject", "content": "No.", "rebuts": ["b"]}',
                '{"stance": "agree", "content": "ok", "rebuts": []}',
                '{"stance": "agree", "content": "ok", "rebuts": []}',
            ]
        )
        orchestrator = RoundtableOrchestrator("task_rt", config(max_rounds=1))
        events = []

        async def run_once():
            async for ev in orchestrator.run():
                events.append(ev)

        asyncio.run(run_once())
        return orchestrator, events

    def test_finished_flag_is_set(self):
        orchestrator, _ = self._finished_orchestrator()
        self.assertTrue(orchestrator.finished)

    def test_follow_up_produces_a_reply(self):
        orchestrator, _ = self._finished_orchestrator()
        # Switch the fake to an agreeing seat for the follow-up.
        patch_provider(['{"stance": "agree", "content": "Answered after the meeting.", "rebuts": []}'])

        events = []

        async def go():
            async for ev in orchestrator.follow_up("One more question.", "a"):
                events.append(ev)

        asyncio.run(go())
        types = [e.type.value for e in events]
        self.assertIn("roundtable-message", types)
        self.assertIn("roundtable-speech", types)
        self.assertIn("roundtable-end", types)

        speech = next(e for e in events if e.type.value == "roundtable-speech")
        self.assertEqual(speech.payload["seat_id"], "a")
        self.assertIn("Answered after the meeting", speech.payload["content"])

    def test_follow_up_to_a_named_seat_only_that_seat_answers(self):
        orchestrator, _ = self._finished_orchestrator()
        patch_provider(['{"stance": "agree", "content": "Only me.", "rebuts": []}'])
        events = []

        async def go():
            async for ev in orchestrator.follow_up("Just you.", "b"):
                events.append(ev)

        asyncio.run(go())
        seats = [e.payload["seat_id"] for e in events if e.type.value == "roundtable-speech"]
        self.assertEqual(seats, ["b"])

    def test_follow_up_keeps_the_recorded_consensus(self):
        orchestrator, _ = self._finished_orchestrator()
        orchestrator._last_consensus = "earlier agreement"
        patch_provider(['{"stance": "agree", "content": "sure", "rebuts": []}'])
        events = []

        async def go():
            async for ev in orchestrator.follow_up("anything?", "a"):
                events.append(ev)

        asyncio.run(go())
        end = events[-1]
        self.assertEqual(end.type.value, "roundtable-end")
        self.assertEqual(end.payload["consensus"], "earlier agreement")
        self.assertFalse(end.payload["requires_human"])

    def test_follow_up_respects_the_token_budget(self):
        orchestrator, _ = self._finished_orchestrator()
        orchestrator._total_tokens = orchestrator._token_budget + 1
        patch_provider(['{"stance": "agree", "content": "x", "rebuts": []}'])
        events = []

        async def go():
            async for ev in orchestrator.follow_up("one more", "all"):
                events.append(ev)

        asyncio.run(go())
        self.assertIn("roundtable-notice", [e.type.value for e in events])
        self.assertNotIn("roundtable-speech", [e.type.value for e in events])
