"""DAG task planning: turn one complex instruction into an ordered graph.

A request like "check the weather, and if it rains send a QQ message and turn on
the humidifier" cannot be executed one step at a time, because two of its steps are
*conditional* on the first. The model produces a plan as a DAG instead, the harness
validates it, and execution walks a topological order with each node receiving the
outputs of its dependencies.

The plan is data, so the hard work is **validation**. A model-supplied graph is
untrusted input, and three failure modes matter:

* **Cycles** - a graph that depends on itself can never run. Rejected at plan time,
  not discovered as a hang.
* **Unknown tools** - a node naming a tool that does not exist would fail at
  execution, after earlier nodes have already caused side effects. Rejected up
  front, before anything runs.
* **Oversized graphs** - unbounded nodes are an unbounded cost and an unbounded
  number of side effects. Capped.

Conditional edges are supported explicitly rather than by convention: a node may
carry ``when``, an expression over a dependency's result. This is what lets the
rain branch stay inside the graph instead of needing a second request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

MAX_NODES = 32
MAX_ID = 64
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,%d}$" % (MAX_ID - 1))

# A `when` expression is deliberately tiny: `node_id` optionally followed by a
# comparison against a literal. No expression evaluator is involved, so a plan
# cannot execute arbitrary text.
_WHEN_RE = re.compile(
    r"""^\s*(?P<ref>[A-Za-z][A-Za-z0-9_.-]*)\s*
        (?:(?P<op>==|!=|=~|contains|exists)\s*
           (?:(?P<quote>['"])(?P<text>.*?)(?P=quote)|(?P<bare>[^\s'"]+))
        )?\s*$""",
    re.VERBOSE,
)


class PlanError(ValueError):
    """The plan is unusable. Raised before any step runs."""


@dataclass
class PlanNode:
    """One step: run ``tool`` with ``args``, possibly only when ``when`` holds."""

    id: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    # Optional gate over a dependency's result: "rain == 'yes'" or "rain".
    when: str = ""
    description: str = ""
    # Filled during execution.
    status: str = "pending"  # pending | running | done | failed | skipped
    output: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "depends_on": list(self.depends_on),
            "status": self.status,
        }
        if self.when:
            out["when"] = self.when
        if self.description:
            out["description"] = self.description
        if self.output:
            out["output"] = self.output[:2000]
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Plan:
    """A validated DAG."""

    goal: str = ""
    nodes: List[PlanNode] = field(default_factory=list)

    def by_id(self, node_id: str) -> Optional[PlanNode]:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "nodes": [node.to_dict() for node in self.nodes]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ------------------------------------------------------------------- parsing
def parse_plan(raw: Any) -> Plan:
    """Build a :class:`Plan` from model output (a dict or a JSON string)."""
    data = raw
    if isinstance(raw, str):
        data = _extract_json(raw)
    if not isinstance(data, dict):
        raise PlanError("the plan must be a JSON object")

    raw_nodes = data.get("steps") or data.get("nodes") or data.get("tasks")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PlanError("the plan must contain a non-empty 'steps' list")

    nodes: List[PlanNode] = []
    for index, entry in enumerate(raw_nodes):
        if not isinstance(entry, dict):
            raise PlanError(f"step {index + 1} is not an object")

        node_id = str(entry.get("id") or f"step{index + 1}").strip()
        if not ID_PATTERN.match(node_id):
            raise PlanError(
                f"step id {node_id!r} is invalid; use letters, digits, '_', '-', '.'"
            )

        tool = str(entry.get("tool") or entry.get("tool_name") or "").strip()
        if not tool:
            raise PlanError(f"step {node_id!r} does not name a tool")

        args = entry.get("args") or entry.get("arguments") or {}
        if not isinstance(args, dict):
            raise PlanError(f"step {node_id!r} has non-object args")

        depends = entry.get("depends_on") or entry.get("after") or entry.get("deps") or []
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, list):
            raise PlanError(f"step {node_id!r} has an invalid depends_on")

        when = str(entry.get("when") or entry.get("condition") or "").strip()
        if when:
            match = _WHEN_RE.match(when)
            if not match:
                raise PlanError(
                    f"step {node_id!r} has an unreadable condition {when!r}. Use "
                    "'<step_id>', '<step_id> == \"value\"', or '<step_id> contains \"text\"'."
                )
            # A condition may only look at a declared dependency, so it can never
            # read a value that was not produced upstream.
            ref = match.group("ref")
            if ref not in [str(dep) for dep in depends]:
                raise PlanError(
                    f"step {node_id!r} conditions on {ref!r} but does not depend on it"
                )

        nodes.append(
            PlanNode(
                id=node_id,
                tool=tool,
                args=dict(args),
                depends_on=[str(dep).strip() for dep in depends],
                when=when,
                description=str(entry.get("description") or entry.get("why") or "").strip(),
            )
        )

    plan = Plan(goal=str(data.get("goal") or data.get("instruction") or "").strip(), nodes=nodes)
    validate_plan(plan)
    return plan


def _extract_json(text: str) -> Any:
    """Pull the first JSON object out of a model reply.

    Models wrap plans in prose or fences constantly, so this tolerates both rather
    than failing on a message that contains a perfectly good plan.
    """
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise PlanError("no JSON object found in the plan")


# ----------------------------------------------------------------- validation
def validate_plan(plan: Plan) -> None:
    """Reject anything that cannot execute. Raises :class:`PlanError`."""
    if len(plan.nodes) > MAX_NODES:
        raise PlanError(f"the plan has {len(plan.nodes)} steps; the limit is {MAX_NODES}")

    ids: Set[str] = set()
    for node in plan.nodes:
        if node.id in ids:
            raise PlanError(f"duplicate step id {node.id!r}")
        ids.add(node.id)

    for node in plan.nodes:
        for dep in node.depends_on:
            if dep not in ids:
                raise PlanError(f"step {node.id!r} depends on unknown step {dep!r}")
            if dep == node.id:
                raise PlanError(f"step {node.id!r} depends on itself")

    order = topological_order(plan)  # raises on cycles


def topological_order(plan: Plan) -> List[PlanNode]:
    """Kahn's algorithm. Stable: ties keep the model's original order.

    Raising on a cycle here means execution never starts a graph that cannot
    finish, which is the difference between a clear error and a hung task.
    """
    index = {node.id: position for position, node in enumerate(plan.nodes)}
    indegree: Dict[str, int] = {node.id: 0 for node in plan.nodes}
    dependents: Dict[str, List[str]] = {node.id: [] for node in plan.nodes}

    for node in plan.nodes:
        for dep in node.depends_on:
            indegree[node.id] += 1
            dependents[dep].append(node.id)

    ready = sorted(
        (node_id for node_id, count in indegree.items() if count == 0),
        key=lambda node_id: index[node_id],
    )
    ordered: List[PlanNode] = []
    by_id = {node.id: node for node in plan.nodes}

    while ready:
        current = ready.pop(0)
        ordered.append(by_id[current])
        for child in dependents[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda node_id: index[node_id])

    if len(ordered) != len(plan.nodes):
        stuck = sorted(set(by_id) - {node.id for node in ordered})
        raise PlanError(f"the plan contains a cycle: {', '.join(stuck)}")
    return ordered


# ---------------------------------------------------------------- conditions
def evaluate_when(expression: str, outputs: Dict[str, str]) -> Tuple[bool, str]:
    """Resolve a ``when`` gate against dependency outputs.

    Absent machinery on purpose: no ``eval``, no expression language. The grammar
    is fixed by ``_WHEN_RE`` and every branch is a string comparison.
    """
    if not expression:
        return True, ""

    match = _WHEN_RE.match(expression)
    if not match:
        return False, f"unreadable condition: {expression}"

    ref = match.group("ref")
    op = match.group("op")
    expected = match.group("text") if match.group("quote") else match.group("bare")

    if ref not in outputs:
        return False, f"condition refers to {ref}, which has not run"

    actual = outputs[ref]
    if op is None:
        # Bare reference: truthy when the step produced something non-empty.
        return bool(actual.strip()), ""

    if op == "exists":
        return bool(actual.strip()), ""

    if op == "contains":
        return (expected or "") in actual, ""

    target = expected or ""
    if op == "==":
        return actual.strip() == target, ""
    if op == "!=":
        return actual.strip() != target, ""
    if op == "=~":
        try:
            return re.search(target, actual) is not None, ""
        except re.error:
            return False, f"invalid pattern in condition: {target}"
    return False, f"unknown operator: {op}"


# --------------------------------------------------------------- plan prompt
PLANNER_SYSTEM_PROMPT = """You are a task planner. Decompose the user's request into a small \
directed acyclic graph of steps.

Reply with JSON only, in this exact shape:

{
  "goal": "one sentence restating the objective",
  "steps": [
    {"id": "check_weather", "tool": "web_search", "args": {"query": "Shanghai weather today"},
     "description": "Find out whether it will rain"},
    {"id": "notify", "tool": "run_python", "args": {"code": "..."},
     "depends_on": ["check_weather"], "when": "check_weather contains \\"rain\\"",
     "description": "Send the reminder only if it rains"}
  ]
}

Rules:
- Every step needs a unique id and a real tool name.
- `depends_on` lists ids that must finish first. A step cannot depend on itself.
- `when` is optional and may only reference a step in `depends_on`. Allowed forms:
  `step_id` (true when it produced output), `step_id == "text"`,
  `step_id contains "text"`, `step_id =~ "regex"`.
- Prefer the fewest steps that do the job. Independent steps should have no
  dependency between them so they can run in parallel.
- Do not invent tools. If a step needs a capability that no tool provides, say so
  in `description` and leave it out.
- The graph must be acyclic. Never plan more than 32 steps."""


# ------------------------------------------------------------------- planner
class TaskPlanner:
    """Asks the model for a DAG, validates it, and exposes the run order.

    ``run_step`` is supplied by the caller and receives ``(node, upstream_outputs)``
    so the harness, not this module, decides how a tool is actually invoked. That
    keeps the planner free of tool wiring and testable with a stub.
    """

    def __init__(
        self,
        ask_model: Callable[[str], str],
        available_tools: Optional[Iterable[str]] = None,
        max_retries: int = 1,
    ) -> None:
        self._ask = ask_model
        self._tools = set(available_tools or [])
        self._max_retries = max(0, int(max_retries))

    def plan(self, instruction: str) -> Plan:
        """Produce a validated plan, retrying once on an unusable answer."""
        prompt = f"{PLANNER_SYSTEM_PROMPT}\n\nRequest:\n{instruction}\n"
        last_error: Optional[PlanError] = None

        for attempt in range(self._max_retries + 1):
            reply = self._ask(prompt if attempt == 0 else self._retry_prompt(instruction, last_error))
            try:
                plan = parse_plan(reply)
            except PlanError as exc:
                last_error = exc
                continue
            self._check_tools(plan)
            return plan

        raise PlanError(f"the model could not produce a usable plan: {last_error}")

    def _retry_prompt(self, instruction: str, error: Optional[PlanError]) -> str:
        return (
            f"{PLANNER_SYSTEM_PROMPT}\n\n"
            f"Request:\n{instruction}\n\n"
            f"Your previous plan was rejected: {error}\n"
            "Reply with corrected JSON only.\n"
        )

    def _check_tools(self, plan: Plan) -> None:
        """Reject nodes naming tools that do not exist, before anything runs.

        Catching this at plan time matters: if node 3 fails because its tool is
        missing, nodes 1 and 2 have already done whatever they did.
        """
        if not self._tools:
            return
        unknown = sorted({node.tool for node in plan.nodes if node.tool not in self._tools})
        if unknown:
            raise PlanError(
                "the plan uses tools that do not exist: "
                + ", ".join(unknown)
                + ". Available tools: "
                + ", ".join(sorted(self._tools))
            )

    # -- execution ---------------------------------------------------------
    async def execute(
        self,
        plan: Plan,
        run_step: Callable[[PlanNode, Dict[str, str]], Any],
    ) -> List[PlanNode]:
        """Run the plan in topological order.

        A failed step does not abort the whole plan: its dependents are skipped and
        independent branches still run. That is usually what the user wants - one
        broken lookup should not cancel the rest of the work.
        """
        import inspect

        ordered = topological_order(plan)
        outputs: Dict[str, str] = {}

        for node in ordered:
            upstream = {dep: outputs.get(dep, "") for dep in node.depends_on}

            if any(plan.by_id(dep) and plan.by_id(dep).status != "done" for dep in node.depends_on):
                node.status = "skipped"
                node.error = "an upstream step did not complete"
                continue

            ok, reason = evaluate_when(node.when, upstream)
            if not ok:
                node.status = "skipped"
                node.error = reason or "condition was false"
                continue

            node.status = "running"
            try:
                result = run_step(node, upstream)
                if inspect.isawaitable(result):
                    result = await result
                node.output = str(result if result is not None else "")
                node.status = "done"
            except Exception as exc:  # noqa: BLE001 - one step must not kill the plan
                node.status = "failed"
                node.error = f"{type(exc).__name__}: {exc}"

            outputs[node.id] = node.output if node.status == "done" else ""

        return ordered


__all__ = [
    "TaskPlanner",
    "Plan",
    "PlanNode",
    "PlanError",
    "parse_plan",
    "validate_plan",
    "topological_order",
    "evaluate_when",
    "PLANNER_SYSTEM_PROMPT",
    "MAX_NODES",
]