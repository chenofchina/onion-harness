"""Layer 4 - functional plugins (executors).

Executors turn a model-supplied ``{code, ...}`` request into a sandbox command,
run it, and normalise the outcome into the shared envelope::

    {"stream": ..., "data": {...}, "timestamp": ..., "status": ...}

The sandbox is the only thing that actually spawns processes; elevation is a
task-level property that the executor forwards without ever interpreting.
"""

from onion_harness.plugins.base import BaseExecutor, ExecutionOutcome
from onion_harness.plugins.registry import ExecutorRegistry, builtin_executors

__all__ = [
    "BaseExecutor",
    "ExecutionOutcome",
    "ExecutorRegistry",
    "builtin_executors",
]