#!/usr/bin/env python
"""CI guard: dependency direction and packaging hygiene.

Rules enforced:

1. **Inward-only imports.** A layer may import only from itself and strictly
   inner layers. This is the core architectural constraint.
2. **The centre is pure.** ``core/`` imports nothing from Onion and nothing
   third-party; only the standard library.
3. **The engine is contained.** The upstream SDK may only be named inside
   ``harness/bridge.py`` (and the dependency manifest), so it can never leak
   into user-facing code, config keys, logs or error strings.
4. **The GUI never imports Python.** Frontend code talks HTTP only.

Exit code is non-zero on any violation so CI fails loudly.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "onion_harness"

# Layer rank: lower rank = more inner. An import of a *higher* rank is illegal.
RANK: Dict[str, int] = {
    "core": 0,
    "sandbox": 1,
    "ledger": 1,
    "memory": 1,
    "plugins": 2,
    "harness": 2,
    "api": 3,
    "app": 4,
}

# Explicit capability edges. Imports must be a subset of `own + inner`.
# (`plugins` and `harness` are siblings at rank 2 and must not import each other.)
ALLOWED: Dict[str, Set[str]] = {
    "core": set(),
    "sandbox": {"core"},
    "ledger": {"core"},
    "memory": {"core"},
    "plugins": {"core", "sandbox"},
    "harness": {"core", "ledger", "memory"},
    "api": {"core", "sandbox", "ledger", "memory", "plugins", "harness"},
    "app": {"core", "sandbox", "ledger", "memory", "plugins", "harness", "api"},
}

STDLIB_ALLOWED = set(sys.stdlib_module_names) | {"__future__", "typing_extensions"}

ENGINE_TOKEN = "loong" + "_agent"
ENGINE_FILES_ALLOWED = {PKG / "harness" / "bridge.py"}
ENGINE_PATTERN = re.compile(re.escape(ENGINE_TOKEN))
MANIFEST_FILES = {ROOT / "pyproject.toml"}


def iter_modules(base: Path) -> Iterable[Tuple[str, Path]]:
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base)
        layer = rel.parts[0] if len(rel.parts) > 1 else ""
        if layer in RANK:
            yield layer, path


def imported_onion_modules(tree: ast.AST) -> Set[str]:
    """Collect the second path component of every `onion_harness.X` import."""
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "onion_harness" and len(parts) > 1:
                found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "onion_harness" and len(parts) > 1:
                    found.add(parts[1])
    return found


def top_level_imports(tree: ast.AST) -> Set[str]:
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
    return found


def check_direction() -> List[str]:
    problems: List[str] = []
    for layer, path in iter_modules(PKG):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"{rel(path)}: syntax error: {exc}")
            continue

        targets = imported_onion_modules(tree)
        allowed = ALLOWED[layer] | {layer}
        for target in sorted(targets):
            if target not in allowed:
                problems.append(
                    f"{rel(path)}: layer '{layer}' imports outer/unrelated layer "
                    f"'{target}' (allowed: {sorted(allowed)})"
                )
    return problems


def check_core_purity() -> List[str]:
    problems: List[str] = []
    core_dir = PKG / "core"
    for path in sorted(core_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in sorted(top_level_imports(tree)):
            if module in STDLIB_ALLOWED:
                continue
            if module == "onion_harness":
                continue
            problems.append(
                f"{rel(path)}: core must be dependency-free but imports '{module}'"
            )
    return problems


def check_engine_containment() -> List[str]:
    problems: List[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in {".venv", "node_modules", "build", "dist", "__pycache__"} for part in path.parts):
            continue
        if path in ENGINE_FILES_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ENGINE_PATTERN.search(line):
                problems.append(
                    f"{rel(path)}:{lineno}: engine package name leaked outside the bridge"
                )
    for path in sorted((ROOT / "frontend" / "src").rglob("*")):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".css", ".html"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if ENGINE_PATTERN.search(text):
            problems.append(f"{rel(path)}: engine package name leaked into the GUI")
    return problems


def check_gui_boundary() -> List[str]:
    """The GUI must reach the backend over HTTP only."""
    problems: List[str] = []
    src = ROOT / "frontend" / "src"
    if not src.exists():
        return problems
    for path in sorted(src.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\b(pywebview|from\s+onion_harness|require\(['\"]onion_harness)", text):
            problems.append(f"{rel(path)}: GUI imports Python directly")
    return problems


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def main() -> int:
    checks = [
        ("dependency direction", check_direction),
        ("core purity", check_core_purity),
        ("engine containment", check_engine_containment),
        ("GUI boundary", check_gui_boundary),
    ]
    failures = 0
    for name, check in checks:
        problems = check()
        if problems:
            failures += len(problems)
            print(f"FAIL  {name}")
            for problem in problems:
                print(f"      {problem}")
        else:
            print(f"ok    {name}")

    if failures:
        print(f"\n{failures} architecture violation(s).")
        return 1
    print("\nArchitecture checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())