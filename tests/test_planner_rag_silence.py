"""DAG planner, RAG++, and the silent-execution red line.

Two groups of assertions matter most.

*Planner*: a model-supplied graph is untrusted input. Cycles, unknown steps,
unknown tools and self-dependencies must be rejected **before** anything runs,
because a plan that fails halfway has already caused side effects.

*Silent execution*: the fix for the black console is a set of flags. These tests
assert the flags are actually requested, because "the console did not appear" is
not something a test can observe - but "CREATE_NO_WINDOW was passed" is.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from onion_harness.harness.planner import (
    MAX_NODES,
    PlanError,
    TaskPlanner,
    evaluate_when,
    parse_plan,
    topological_order,
)
from onion_harness.harness.rag import (
    RagPipeline,
    Snippet,
    _coerce_scores,
    _normalise_queries,
    build_rerank_prompt,
    build_rewrite_prompt,
    lexical_score,
)
from onion_harness.memory.model import Memory, MemoryKind
from onion_harness.sandbox.safe_exec import (
    CREATE_NO_WINDOW,
    CREATE_NEW_PROCESS_GROUP,
    SafeCommandExecutor,
    _clamp_timeout,
    cmd_argv,
    powershell_argv,
    spawn_kwargs,
)


# -------------------------------------------------------------------- planner
class PlanParsingTest(unittest.TestCase):
    def test_parses_a_plain_plan(self):
        plan = parse_plan(
            {"goal": "g", "steps": [{"id": "a", "tool": "web_search", "args": {"query": "x"}}]}
        )
        self.assertEqual(plan.goal, "g")
        self.assertEqual(plan.nodes[0].tool, "web_search")
        self.assertEqual(plan.nodes[0].args, {"query": "x"})

    def test_accepts_json_inside_prose_and_fences(self):
        reply = (
            "Here is the plan:\n```json\n"
            '{"goal":"g","steps":[{"id":"a","tool":"t"}]}\n```\nDone.'
        )
        self.assertEqual(parse_plan(reply).nodes[0].id, "a")

    def test_accepts_alternative_key_names(self):
        plan = parse_plan({"nodes": [{"id": "a", "tool_name": "t", "arguments": {}}]})
        self.assertEqual(plan.nodes[0].tool, "t")

    def test_string_dependency_is_coerced(self):
        plan = parse_plan(
            {"steps": [{"id": "a", "tool": "t"}, {"id": "b", "tool": "t", "depends_on": "a"}]}
        )
        self.assertEqual(plan.nodes[1].depends_on, ["a"])

    def test_missing_steps_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan({"goal": "g"})

    def test_no_json_at_all_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan("I cannot plan this")

    def test_bad_id_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan({"steps": [{"id": "9 bad id!", "tool": "t"}]})

    def test_missing_tool_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan({"steps": [{"id": "a"}]})

    def test_non_object_args_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan({"steps": [{"id": "a", "tool": "t", "args": "nope"}]})


class PlanValidationTest(unittest.TestCase):
    def test_duplicate_ids_rejected(self):
        with self.assertRaises(PlanError) as ctx:
            parse_plan(
                {"steps": [{"id": "a", "tool": "t"}, {"id": "a", "tool": "t"}]}
            )
        self.assertIn("duplicate", str(ctx.exception))

    def test_self_dependency_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan({"steps": [{"id": "a", "tool": "t", "depends_on": ["a"]}]})

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(PlanError) as ctx:
            parse_plan({"steps": [{"id": "a", "tool": "t", "depends_on": ["ghost"]}]})
        self.assertIn("unknown", str(ctx.exception))

    def test_cycle_rejected(self):
        with self.assertRaises(PlanError) as ctx:
            parse_plan(
                {
                    "steps": [
                        {"id": "a", "tool": "t", "depends_on": ["b"]},
                        {"id": "b", "tool": "t", "depends_on": ["a"]},
                    ]
                }
            )
        self.assertIn("cycle", str(ctx.exception))

    def test_node_limit(self):
        steps = [{"id": f"s{i}", "tool": "t"} for i in range(MAX_NODES + 1)]
        with self.assertRaises(PlanError):
            parse_plan({"steps": steps})

    def test_condition_must_reference_a_dependency(self):
        with self.assertRaises(PlanError) as ctx:
            parse_plan(
                {
                    "steps": [
                        {"id": "a", "tool": "t"},
                        {"id": "b", "tool": "t", "depends_on": ["a"], "when": "c contains 'x'"},
                    ]
                }
            )
        self.assertIn("does not depend on it", str(ctx.exception))

    def test_unreadable_condition_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan(
                {
                    "steps": [
                        {"id": "a", "tool": "t"},
                        {"id": "b", "tool": "t", "depends_on": ["a"], "when": "!!! >>>"},
                    ]
                }
            )


class TopologyTest(unittest.TestCase):
    def test_dependencies_come_first(self):
        plan = parse_plan(
            {
                "steps": [
                    {"id": "c", "tool": "t", "depends_on": ["b"]},
                    {"id": "b", "tool": "t", "depends_on": ["a"]},
                    {"id": "a", "tool": "t"},
                ]
            }
        )
        self.assertEqual([node.id for node in topological_order(plan)], ["a", "b", "c"])

    def test_independent_steps_keep_declared_order(self):
        plan = parse_plan({"steps": [{"id": "x", "tool": "t"}, {"id": "y", "tool": "t"}]})
        self.assertEqual([node.id for node in topological_order(plan)], ["x", "y"])

    def test_diamond(self):
        plan = parse_plan(
            {
                "steps": [
                    {"id": "a", "tool": "t"},
                    {"id": "b", "tool": "t", "depends_on": ["a"]},
                    {"id": "c", "tool": "t", "depends_on": ["a"]},
                    {"id": "d", "tool": "t", "depends_on": ["b", "c"]},
                ]
            }
        )
        order = [node.id for node in topological_order(plan)]
        self.assertEqual(order[0], "a")
        self.assertEqual(order[-1], "d")


class ConditionTest(unittest.TestCase):
    def test_bare_reference_is_truthy_on_output(self):
        self.assertEqual(evaluate_when("a", {"a": "something"})[0], True)
        self.assertEqual(evaluate_when("a", {"a": "  "})[0], False)

    def test_equality(self):
        self.assertTrue(evaluate_when('a == "rain"', {"a": "rain"})[0])
        self.assertFalse(evaluate_when('a == "rain"', {"a": "sun"})[0])

    def test_inequality(self):
        self.assertTrue(evaluate_when('a != "rain"', {"a": "sun"})[0])

    def test_contains(self):
        self.assertTrue(evaluate_when('a contains "rain"', {"a": "it will rain"})[0])
        self.assertFalse(evaluate_when('a contains "rain"', {"a": "sunny"})[0])

    def test_regex(self):
        self.assertTrue(evaluate_when('a =~ "r.in"', {"a": "rain"})[0])
        self.assertFalse(evaluate_when('a =~ "^x"', {"a": "rain"})[0])

    def test_invalid_regex_is_false_not_a_crash(self):
        self.assertEqual(evaluate_when('a =~ "["', {"a": "x"})[0], False)

    def test_missing_reference_is_false(self):
        self.assertEqual(evaluate_when("a", {})[0], False)

    def test_empty_expression_is_true(self):
        self.assertTrue(evaluate_when("", {})[0])


class PlannerRunTest(unittest.TestCase):
    def _planner(self, reply, tools=None):
        return TaskPlanner(lambda prompt: reply, available_tools=tools or [])

    def test_plan_is_produced(self):
        plan = self._planner('{"steps":[{"id":"a","tool":"t"}]}').plan("do it")
        self.assertEqual(plan.nodes[0].id, "a")

    def test_retry_after_a_bad_plan(self):
        replies = iter(["not json", '{"steps":[{"id":"a","tool":"t"}]}'])
        planner = TaskPlanner(lambda prompt: next(replies), max_retries=1)
        self.assertEqual(planner.plan("x").nodes[0].id, "a")

    def test_gives_up_after_retries(self):
        planner = TaskPlanner(lambda prompt: "nope", max_retries=1)
        with self.assertRaises(PlanError):
            planner.plan("x")

    def test_unknown_tool_rejected_before_running(self):
        planner = self._planner(
            '{"steps":[{"id":"a","tool":"does_not_exist"}]}', tools=["web_search"]
        )
        with self.assertRaises(PlanError) as ctx:
            planner.plan("x")
        self.assertIn("do not exist", str(ctx.exception))


class PlannerExecutionTest(unittest.TestCase):
    def _plan(self):
        return parse_plan(
            {
                "steps": [
                    {"id": "weather", "tool": "w"},
                    {"id": "notify", "tool": "t", "depends_on": ["weather"], "when": 'weather contains "rain"'},
                    {"id": "humid", "tool": "t", "depends_on": ["weather"], "when": 'weather contains "rain"'},
                ]
            }
        )

    def _run(self, plan, weather_text, fail_on=None):
        ran = []

        async def step(node, upstream):
            if fail_on and node.id == fail_on:
                raise RuntimeError("boom")
            ran.append(node.id)
            if node.id == "weather":
                return weather_text
            return "did " + node.id

        ordered = asyncio.run(TaskPlanner(lambda p: "", []).execute(plan, step))
        return ordered, ran

    def test_rain_runs_the_branch(self):
        ordered, ran = self._run(self._plan(), "heavy rain expected")
        self.assertEqual(ran, ["weather", "notify", "humid"])
        self.assertTrue(all(node.status == "done" for node in ordered))

    def test_dry_skips_the_branch(self):
        ordered, ran = self._run(self._plan(), "sunny all week")
        self.assertEqual(ran, ["weather"])
        by_id = {node.id: node for node in ordered}
        self.assertEqual(by_id["notify"].status, "skipped")
        self.assertIn("condition", by_id["notify"].error)

    def test_a_failed_step_skips_its_dependents(self):
        plan = parse_plan(
            {
                "steps": [
                    {"id": "a", "tool": "t"},
                    {"id": "b", "tool": "t", "depends_on": ["a"]},
                ]
            }
        )
        ordered, _ran = self._run(plan, "", fail_on="a")
        by_id = {node.id: node for node in ordered}
        self.assertEqual(by_id["a"].status, "failed")
        self.assertEqual(by_id["b"].status, "skipped")

    def test_independent_branch_still_runs_after_a_failure(self):
        plan = parse_plan(
            {
                "steps": [
                    {"id": "a", "tool": "t"},
                    {"id": "b", "tool": "t", "depends_on": ["a"]},
                    {"id": "c", "tool": "t"},
                ]
            }
        )
        _ordered, ran = self._run(plan, "", fail_on="a")
        self.assertIn("c", ran)

    def test_upstream_output_is_passed_down(self):
        seen = {}

        async def step(node, upstream):
            seen[node.id] = dict(upstream)
            return "value-" + node.id

        plan = parse_plan(
            {"steps": [{"id": "a", "tool": "t"}, {"id": "b", "tool": "t", "depends_on": ["a"]}]}
        )
        asyncio.run(TaskPlanner(lambda p: "", []).execute(plan, step))
        self.assertEqual(seen["b"], {"a": "value-a"})


# --------------------------------------------------------------------- RAG++
class LexicalTest(unittest.TestCase):
    def test_relevant_scores_higher_than_noise(self):
        good = lexical_score("build faster", "the build took 40s, caching helped")
        noise = lexical_score("build faster", "the cat sat on the mat")
        self.assertGreater(good, noise)

    def test_no_overlap_is_zero(self):
        self.assertEqual(lexical_score("alpha", "beta"), 0.0)

    def test_empty_question_is_zero(self):
        self.assertEqual(lexical_score("", "anything"), 0.0)

    def test_chinese_is_handled_via_bigrams(self):
        self.assertGreater(lexical_score("构建 速度", "构建速度优化"), 0)


class QueryNormalisationTest(unittest.TestCase):
    def test_list(self):
        self.assertEqual(_normalise_queries(["a", "b"]), ["a", "b"])

    def test_json_array_string(self):
        self.assertEqual(_normalise_queries('["a","b"]'), ["a", "b"])

    def test_bullet_lines(self):
        self.assertEqual(_normalise_queries("- a\n- b"), ["a", "b"])

    def test_none(self):
        self.assertEqual(_normalise_queries(None), [])


class ScoreCoercionTest(unittest.TestCase):
    def test_list_of_numbers(self):
        self.assertEqual(_coerce_scores([0, 10], 2), [0.0, 1.0])

    def test_json_string(self):
        self.assertEqual(_coerce_scores("[0, 10]", 2), [0.0, 1.0])

    def test_numbers_in_prose(self):
        self.assertEqual(_coerce_scores("scores: 0, 10", 2), [0.0, 1.0])

    def test_wrong_length_is_none(self):
        self.assertIsNone(_coerce_scores([1, 2, 3], 2))

    def test_non_numeric_is_none(self):
        self.assertIsNone(_coerce_scores(["a", "b"], 2))

    def test_all_equal_is_degenerate_but_safe(self):
        self.assertEqual(_coerce_scores([5, 5], 2), [1.0, 1.0])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.memories = [
            Memory(text="build uses webpack, enabling cache cut it to 12s", kind=MemoryKind.FACT, id="m1"),
            Memory(text="user prefers PowerShell for scripts", kind=MemoryKind.PREFERENCE, id="m2"),
            Memory(text="compile flags -O2 gave a 15% speedup", kind=MemoryKind.FACT, id="m3"),
        ]

    def _recall(self, query, limit):
        words = [w for w in query.lower().split() if len(w) > 2]
        hits = [m for m in self.memories if any(w in m.text.lower() for w in words)]
        return hits[:limit]

    def test_citations_are_numbered_from_one(self):
        async def rewrite(q):
            return ["webpack build cache"]

        pipeline = RagPipeline(self._recall, rewrite=rewrite)
        result = asyncio.run(pipeline.retrieve("why is my build slow"))
        self.assertTrue(result.snippets)
        self.assertEqual(result.snippets[0].id, "1")
        self.assertIn("[1]", result.compose_prompt())

    def test_rewrite_adds_probes_but_keeps_the_original(self):
        async def rewrite(q):
            return ["webpack cache", "compile speedup"]

        pipeline = RagPipeline(self._recall, rewrite=rewrite)
        result = asyncio.run(pipeline.retrieve("build slow"))
        self.assertEqual(result.queries[0], "build slow", "the user's words must stay first")
        self.assertEqual(len(result.queries), 3)
        self.assertTrue(result.rewritten)

    def test_noise_is_filtered_out(self):
        async def rewrite(q):
            return ["webpack"]

        pipeline = RagPipeline(self._recall, rewrite=rewrite)
        result = asyncio.run(pipeline.retrieve("webpack cache"))
        for snippet in result.snippets:
            self.assertGreater(snippet.score, 0.0)

    def test_rerank_reorders(self):
        async def rerank(question, passages):
            # Deliberately reverse: the last passage should end up first.
            return list(range(len(passages), 0, -1))

        async def rewrite(q):
            return ["webpack cache compile speedup"]

        pipeline = RagPipeline(self._recall, rewrite=rewrite, rerank=rerank)
        result = asyncio.run(pipeline.retrieve("build cache speedup"))
        self.assertTrue(result.reranked)

    def test_reranker_failure_degrades_not_breaks(self):
        async def boom(question, passages):
            raise RuntimeError("model down")

        async def rewrite(q):
            # Two probes so more than one candidate is pooled, which is the case
            # where the reranker is actually consulted.
            return ["webpack build", "compile speedup"]

        pipeline = RagPipeline(self._recall, rewrite=rewrite, rerank=boom)
        result = asyncio.run(pipeline.retrieve("build cache speedup"))
        self.assertFalse(result.reranked)
        self.assertIn("unavailable", result.degraded)
        self.assertTrue(result.snippets, "lexical fallback must still return results")

    def test_single_candidate_skips_rerank_and_says_so(self):
        """Reordering one item cannot change anything, so the call is skipped.

        The skip is reported rather than left silent: "reranked: false" with no
        explanation reads like the reranker is broken.
        """
        lists = {"n": 0}

        async def rerank(question, passages):
            lists["n"] += 1
            return list(range(len(passages)))

        async def rewrite(q):
            return ["webpack"]

        # Recall returns exactly one memory for this query.
        pipeline = RagPipeline(
            lambda query, limit: [self.memories[0]], rewrite=rewrite, rerank=rerank
        )
        result = asyncio.run(pipeline.retrieve("webpack cache"))
        self.assertEqual(lists["n"], 0, "the model must not be called for one candidate")
        self.assertIn("only one candidate", result.degraded)

    def test_rewrite_failure_degrades_not_breaks(self):
        async def boom(q):
            raise RuntimeError("model down")

        pipeline = RagPipeline(self._recall, rewrite=boom)
        result = asyncio.run(pipeline.retrieve("webpack cache"))
        self.assertIn("unavailable", result.degraded)
        self.assertEqual(result.queries, ["webpack cache"])
        self.assertTrue(result.snippets)

    def test_no_memory_means_empty_result(self):
        pipeline = RagPipeline(lambda q, n: [])
        result = asyncio.run(pipeline.retrieve("anything"))
        self.assertTrue(result.empty)
        self.assertEqual(result.compose_prompt(), "")

    def test_empty_question(self):
        result = asyncio.run(RagPipeline(self._recall).retrieve("   "))
        self.assertTrue(result.empty)

    def test_prompt_instructs_citation_and_no_invention(self):
        async def rewrite(q):
            return ["webpack"]

        pipeline = RagPipeline(self._recall, rewrite=rewrite)
        prompt = asyncio.run(pipeline.retrieve("webpack cache")).compose_prompt()
        self.assertIn("cite", prompt.lower())
        self.assertIn("instead of filling the gap from memory", prompt)

    def test_prompts_are_well_formed(self):
        self.assertIn("Question:", build_rewrite_prompt("q"))
        self.assertIn("[1]", build_rerank_prompt("q", ["passage"]))


# -------------------------------------------------------- silent execution
class SpawnFlagTest(unittest.TestCase):
    """The red line: no console window, ever."""

    def test_no_window_flag_is_requested(self):
        with mock.patch("onion_harness.core.process._WINDOWS", True):
            kwargs = spawn_kwargs()
            flags = kwargs["creationflags"]
            self.assertTrue(flags & CREATE_NO_WINDOW, "CREATE_NO_WINDOW must be set")
            self.assertTrue(flags & CREATE_NEW_PROCESS_GROUP, "tree-kill needs a group")

    def test_startupinfo_hides_the_window(self):
        with mock.patch("onion_harness.core.process._WINDOWS", True):
            info = spawn_kwargs()["startupinfo"]
            self.assertTrue(info.dwFlags & 1)  # STARTF_USESHOWWINDOW
            self.assertEqual(info.wShowWindow, 0)  # SW_HIDE

    def test_no_group_variant_still_hides(self):
        with mock.patch("onion_harness.core.process._WINDOWS", True):
            flags = spawn_kwargs(new_group=False)["creationflags"]
            self.assertTrue(flags & CREATE_NO_WINDOW)
            self.assertFalse(flags & CREATE_NEW_PROCESS_GROUP)

    def test_posix_uses_a_new_session(self):
        with mock.patch("onion_harness.core.process._WINDOWS", False):
            self.assertEqual(spawn_kwargs(), {"start_new_session": True})
            self.assertEqual(spawn_kwargs(new_group=False), {})

    def test_powershell_argv_is_non_interactive_and_hidden(self):
        argv = powershell_argv("x.ps1")
        for flag in ("-NoProfile", "-NonInteractive", "-NoLogo"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-WindowStyle") + 1], "Hidden")
        self.assertEqual(argv[-1], "x.ps1")

    def test_cmd_argv(self):
        self.assertEqual(cmd_argv("x.cmd"), ["cmd.exe", "/d", "/c", "x.cmd"])

    def test_timeout_is_clamped(self):
        self.assertEqual(_clamp_timeout(0), 300.0)
        self.assertEqual(_clamp_timeout(-5), 300.0)
        self.assertEqual(_clamp_timeout("junk"), 300.0)
        self.assertLessEqual(_clamp_timeout(99_999), 3600.0)
        self.assertGreaterEqual(_clamp_timeout(0.001), 0.1)


class ExecutorBehaviourTest(unittest.TestCase):
    def test_captures_stdout(self):
        outcome = SafeCommandExecutor(timeout=30).run(
            ["python", "-c", "print('hello from child')"]
        )
        self.assertTrue(outcome.ok)
        self.assertIn("hello from child", outcome.stdout)

    def test_captures_stderr(self):
        outcome = SafeCommandExecutor(timeout=30).run(
            ["python", "-c", "import sys; sys.stderr.write('bad thing')"]
        )
        self.assertIn("bad thing", outcome.stderr)

    def test_nonzero_exit_is_reported_not_raised(self):
        outcome = SafeCommandExecutor(timeout=30).run(["python", "-c", "raise SystemExit(3)"])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.exit_code, 3)

    def test_missing_executable_is_reported(self):
        outcome = SafeCommandExecutor(timeout=10).run(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(outcome.ok)
        self.assertIn("not found", outcome.error or "")

    def test_empty_command(self):
        outcome = SafeCommandExecutor().run([])
        self.assertFalse(outcome.ok)

    def test_timeout_kills_the_process(self):
        # Sleeps far past the timeout; the call must return promptly regardless.
        import time

        started = time.perf_counter()
        outcome = SafeCommandExecutor(timeout=1).run(
            ["python", "-c", "import time; time.sleep(30)"], timeout=1
        )
        elapsed = time.perf_counter() - started
        self.assertTrue(outcome.timed_out)
        self.assertLess(elapsed, 20, "the timeout must actually bound the call")

    def test_stdin_is_forwarded(self):
        outcome = SafeCommandExecutor(timeout=20).run(
            ["python", "-c", "import sys; print(sys.stdin.read().upper())"],
            stdin="hello",
        )
        self.assertTrue(outcome.ok)
        self.assertIn("HELLO", outcome.stdout)

    def test_to_result_maps_fields(self):
        result = SafeCommandExecutor(timeout=20).run(
            ["python", "-c", "print('x')"]
        ).to_result()
        self.assertTrue(result.ok)
        self.assertIn("x", result.stdout)


class LauncherWiringTest(unittest.TestCase):
    """The sandbox launcher must use the same flags as the block executor."""

    def test_launcher_imports_the_shared_spawn_kwargs(self):
        import onion_harness.sandbox.launcher as launcher

        source = open(launcher.__file__, encoding="utf-8").read()
        self.assertIn("spawn_kwargs", source)
        self.assertNotIn("subprocess.CREATE_NEW_PROCESS_GROUP", source)

    def test_executors_share_the_argv_builders(self):
        from onion_harness.plugins.cmd_executor import CmdExecutor
        from onion_harness.plugins.powershell_executor import PowerShellExecutor

        ps = PowerShellExecutor.build_command(
            PowerShellExecutor, __import__("pathlib").Path("s.ps1"), 60, __import__("pathlib").Path(".")
        )
        self.assertIn("-WindowStyle", " ".join(ps))
        cmd = CmdExecutor.build_command(
            CmdExecutor, __import__("pathlib").Path("s.cmd"), 60, __import__("pathlib").Path(".")
        )
        self.assertEqual(cmd[0], "cmd.exe")


if __name__ == "__main__":
    unittest.main()