"""MCP tool contract (layer-neutral).

The ledger must validate a proposed tool before it is staged, and the harness
must render it into a real script. Both need the same rules, so the rules live
in core and neither layer imports the other.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")

# Modules a generated tool may import. Deliberately no socket/urllib/requests:
# a generated tool that needs the network should say so explicitly and be
# reviewed, rather than sneak it past the ledger.
ALLOWED_IMPORTS = {
    "__future__",
    "json",
    "sys",
    "os",
    "re",
    "math",
    "datetime",
    "pathlib",
    "dataclasses",
    "typing",
    "collections",
    "itertools",
    "functools",
    "textwrap",
    "hashlib",
    "base64",
    "uuid",
    "random",
    "string",
    "csv",
    "decimal",
    "statistics",
}

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "globals", "locals", "vars"}
FORBIDDEN_ATTRS = {"system", "popen", "spawn", "fork", "remove", "rmtree", "unlink"}
# Referencing one of these as a bare value (e.g. `f = eval`) is the same risk as
# calling it, and is how a naive check gets bypassed.
FORBIDDEN_NAMES = FORBIDDEN_CALLS | {"__builtins__", "__loader__", "__spec__"}
# getattr/setattr with a computed name defeats any static attribute check.
DYNAMIC_ACCESSORS = {"getattr", "setattr", "delattr", "hasattr"}


class McpError(ValueError):
    """Invalid or unsafe generated tool."""


def validate_spec(name: str, parameters: Dict[str, Any], code: str) -> None:
    """Static safety check before anything is written to disk."""
    if not NAME_RE.match(name or ""):
        raise McpError(
            "tool name must be lowercase snake_case, 3-48 chars, starting with a letter"
        )
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise McpError("parameters must be a JSON Schema object with type 'object'")
    if not isinstance(code, str) or not code.strip():
        raise McpError("code body is required")

    # The body must define `handle(arguments)`.
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise McpError(f"generated code has a syntax error: {exc.msg}") from exc

    has_handle = any(
        isinstance(node, ast.FunctionDef) and node.name == "handle" for node in tree.body
    )
    if not has_handle:
        raise McpError("generated code must define `def handle(arguments):`")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise McpError("relative imports are not allowed")
            if node.module:
                _check_module(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in FORBIDDEN_CALLS:
                    raise McpError(f"`{func.id}` is not allowed in a generated tool")
                if func.id in DYNAMIC_ACCESSORS:
                    raise McpError(
                        f"`{func.id}` is not allowed: it can read attributes that a "
                        "static check cannot see"
                    )
        elif isinstance(node, ast.Name):
            # `f = eval` then `f(...)` would slip past a call-only check.
            if node.id in FORBIDDEN_NAMES:
                raise McpError(f"`{node.id}` is not allowed in a generated tool")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                raise McpError(f"`{node.attr}` is not allowed in a generated tool")
            # Dunder access is how sandbox escapes start (__subclasses__,
            # __globals__, __class__). Generated tools have no reason to use it.
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise McpError(
                    f"dunder attribute `{node.attr}` is not allowed in a generated tool"
                )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise McpError("global/nonlocal state is not allowed in a generated tool")


def _check_module(module: str) -> None:
    if module not in ALLOWED_IMPORTS:
        raise McpError(
            f"import `{module}` is not permitted; allowed modules: "
            + ", ".join(sorted(ALLOWED_IMPORTS))
        )


__all__ = [
    "NAME_RE",
    "ALLOWED_IMPORTS",
    "FORBIDDEN_CALLS",
    "FORBIDDEN_ATTRS",
    "McpError",
    "validate_spec",
]
