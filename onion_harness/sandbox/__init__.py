"""Layer 2 - sandbox.

Contract: exactly one entry point, ``Sandbox.execute(task, ctx) -> Result``.

MVP isolation is deliberately coarse:

* **Process-level timeout.** A watchdog kills the whole process *tree* when the
  deadline passes, so a hung child cannot outlive the task.
* **Working-directory isolation.** Every command runs with ``cwd`` forced to a
  resolved workspace directory, and the workspace is validated/created up
  front.  No filesystem or network virtualisation yet; the interface is the
  stable part, the strategy is swappable via :class:`SandboxStrategy`.
* **Output bounding.** stdout/stderr are capped so a runaway process cannot
  exhaust memory or blow up the event stream.
* **Elevation.** UAC handling lives inside the launcher and never leaks into
  the event format or the harness.  Elevated runs redirect output to a temp
  file that is read back after the task ends.

The sandbox knows nothing about models, tools or the GUI.
"""

from onion_harness.sandbox.api import Sandbox, SandboxStrategy
from onion_harness.sandbox.launcher import ProcessLauncher
from onion_harness.sandbox.strategies import (
    NetworkDenyStrategy,
    ProcessIsolationStrategy,
    ReadOnlyWorkspaceStrategy,
)

__all__ = [
    "Sandbox",
    "SandboxStrategy",
    "ProcessLauncher",
    "ProcessIsolationStrategy",
    "NetworkDenyStrategy",
    "ReadOnlyWorkspaceStrategy",
]