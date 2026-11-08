"""Integration: persona stacking and the review gate inside a real runtime run.

These use a fake engine provider, so no network is touched. What they verify is
the wiring: the persona reaches the system prompt in the right position, and a
rejected snippet never reaches the executor.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from onion_harness.core import EventType, Task
from onion_harness.core.persona import PersonaLibrary
from onion_harness.harness import runtime as runtime_module
from onion_harness.harness.bridge import EngineChatResponse, EngineToolCall, EngineUsage
from onion_harness.harness.review import ReviewConfig, ReviewGate
from onion_harness.ledger import ChangeLedger, LedgerStore
from onion_harness.plugins import builtin_executors
from onion_harness.sandbox import Sandbox


class _CaptureProvider:
    """Records the system prompt and can emit one tool call."""

    def __init__(self, script, systems, executed):
        self.model = "fake"
        self.last_reasoning = ""
        self._script = script
        self._i = 0
        self._systems = systems
        self._executed = executed

    def get_model_name(self):
        return self.model

    def clone(self, model=None):
        return self

    async def chat_completion(self, messages, tools=None, **kwargs):
        for message in messages:
            if getattr(message, "type", "") == "system":
                self._systems.append(getattr(message, "text", ""))
        self._executed.append(dict(kwargs))
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return EngineChatResponse(
            text=step.get("text", ""),
            tool_calls=step.get("tool_calls", []),
            usage=EngineUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
            model=self.model,
        )


class _StubReviewer:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def chat_completion(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return type("R", (), {"text": self.answer})()


class _Base(unittest.TestCase):
    script: list = []

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onion-gate-"))
        self.ledger = ChangeLedger(LedgerStore(self.dir / "l.db"))
        self.sandbox = Sandbox(workspace_root=str(self.dir))
        self.executors = builtin_executors(self.sandbox)
        self._original = runtime_module.build_provider
        self.systems: list = []
        self.calls: list = []
        self.out_of_band: list = []

    def tearDown(self):
        runtime_module.build_provider = self._original
        self.ledger.close()

    def _runtime(self, *, reviewer=None, personas=None):
        from onion_harness.harness.runtime import OnionRuntime

        runtime_module.build_provider = lambda *a, **k: _CaptureProvider(
            self.script, self.systems, self.calls
        )
        runtime = OnionRuntime(
            ledger=self.ledger,
            executors=self.executors,
            sandbox=self.sandbox,
            state_provider=lambda: self.ledger.state,
            personas=personas,
            reviewer=reviewer,
        )
        # Review and approval events are emitted from inside the tool call, where
        # the generator is blocked. The API layer publishes them through this
        # listener; the tests collect them the same way so they observe what the
        # GUI actually receives.
        runtime.set_approval_listener(
            lambda _task_id, payload: self.out_of_band.append(payload)
        )
        return runtime

    def _run(self, runtime, prompt="do it", **model_overrides):
        from onion_harness.harness.runtime import build_model_config

        model = build_model_config(
            {"provider_id": "deepseek", "api_key": "sk-test", **model_overrides}
        )
        events = []

        async def consume():
            async for ev in runtime.run_task(Task(prompt=prompt, workspace=str(self.dir)), model):
                events.append(ev)

        asyncio.run(consume())
        return events


class PersonaIntegrationTest(_Base):
    script = [{"text": "done"}]

    def test_persona_overlay_is_in_the_system_prompt(self):
        runtime = self._runtime(personas=PersonaLibrary.load(None))
        events = self._run(runtime, persona_id="operator")
        self.assertTrue(self.systems)
        self.assertIn("Persona: Operator", self.systems[0])
        self.assertIn(EventType.PERSONA_ACTIVE, [ev.type for ev in events])

    def test_default_persona_emits_no_event(self):
        runtime = self._runtime(personas=PersonaLibrary.load(None))
        events = self._run(runtime, persona_id="default")
        self.assertNotIn(EventType.PERSONA_ACTIVE, [ev.type for ev in events])
        self.assertNotIn("Persona:", self.systems[0])

    def test_persona_never_moves_the_mode_contract(self):
        """The Strict contract must remain the last block, even with a persona."""
        runtime = self._runtime(personas=PersonaLibrary.load(None))
        self._run(runtime, persona_id="operator", mode="strict")
        prompt = self.systems[0]
        self.assertLess(prompt.index("Persona: Operator"), prompt.index("Strict coding contract"))

    def test_persona_sampling_reaches_the_provider_call(self):
        runtime = self._runtime(personas=PersonaLibrary.load(None))
        self._run(runtime, persona_id="auditor")
        combined = {key: value for call in self.calls for key, value in call.items()}
        self.assertAlmostEqual(float(combined.get("temperature")), 0.2)
        self.assertAlmostEqual(float(combined.get("top_p")), 0.85)


class ReviewIntegrationTest(_Base):
    # One tool call, then a final answer.
    script = [
        {
            "tool_calls": [
                EngineToolCall(
                    id="c1", name="run_powershell", arguments={"code": "Remove-Item -Recurse x"}
                )
            ]
        },
        {"text": "blocked, asking the user"},
    ]

    def _gate(self, answer):
        reviewer = _StubReviewer(answer)
        gate = ReviewGate(ReviewConfig(), provider_factory=lambda _c: reviewer)
        return gate, reviewer

    def test_reject_blocks_before_the_executor_runs(self):
        gate, reviewer = self._gate("REJECT")
        runtime = self._runtime(reviewer=gate)
        events = self._run(runtime, mode="strict")

        decisions = [item for item in self.out_of_band if item["type"] == "review-decided"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["payload"]["decision"], "reject")
        self.assertFalse(decisions[0]["payload"]["unavailable"])
        self.assertTrue(reviewer.calls)

        # The executor never ran. A blocked call still produces a tool result -
        # that is how the model is told - but the content is the refusal, not
        # command output.
        results = [ev for ev in events if ev.type is EventType.TOOL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertIn("Blocked by automatic safety review", results[0].payload["content"])

    def test_pass_lets_the_executor_run(self):
        # Strict mode is used here so the run does not block on an approval card;
        # that the card appears after a PASS in Standard mode is covered below.
        gate, _ = self._gate("PASS")
        runtime = self._runtime(reviewer=gate)
        events = self._run(runtime, mode="strict")
        decisions = [item for item in self.out_of_band if item["type"] == "review-decided"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["payload"]["decision"], "pass")
        self.assertIn(EventType.TOOL_RESULT, [ev.type for ev in events])

    def test_standard_mode_still_asks_after_a_pass(self):
        """Review is a gate in front of approval, not a replacement for it."""
        gate, _ = self._gate("PASS")
        runtime = self._runtime(reviewer=gate)
        asked = []

        async def _decline(*_args, **_kwargs):
            asked.append(True)
            return False

        runtime._request_approval = _decline
        events = self._run(runtime, mode="standard")
        self.assertTrue(asked, "a passed review must still reach the approval card")
        # Declined: the tool result exists but carries the refusal, not output.
        results = [ev for ev in events if ev.type is EventType.TOOL_RESULT]
        self.assertTrue(results)
        self.assertIn("declined", results[0].payload["content"])

    def test_read_only_snippet_skips_review_entirely(self):
        self.script = [
            {
                "tool_calls": [
                    EngineToolCall(id="c1", name="run_powershell", arguments={"code": "Get-Date"})
                ]
            },
            {"text": "done"},
        ]
        gate, reviewer = self._gate("REJECT")
        runtime = self._runtime(reviewer=gate)
        events = self._run(runtime, mode="strict")
        self.assertEqual([i for i in self.out_of_band if i["type"] == "review-decided"], [])
        self.assertEqual(reviewer.calls, [])
        self.assertIn(EventType.TOOL_RESULT, [ev.type for ev in events])

    def test_no_reviewer_means_no_gate(self):
        runtime = self._runtime(reviewer=None)
        self._run(runtime, mode="strict")
        self.assertEqual([i for i in self.out_of_band if i["type"] == "review-decided"], [])


if __name__ == "__main__":
    unittest.main()