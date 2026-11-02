"""Ratchet: every engine module that builds a pytest argv imports coordinator_core.testing.spawn_depth.

A list/tuple literal carrying adjacent "-m","pytest" (or leading with a pytest executable name) in a
non-test coordinator_core module marks a pytest spawn site; the module must import spawn_depth so the
depth refusal, self-inclusion refusal and child_env stamp are reachable. Test modules are out of the
walk: a raw test-side spawn is bounded by the root conftest's depth stamp, not by this ratchet.
"""
from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]

_PYTEST_NAMES = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe"})
_SPAWN_DEPTH = "coordinator_core.testing.spawn_depth"

# path (relative to coordinator_core, posix) -> reason the argv is never executed.
ALLOWLIST: dict[str, str] = {
    "bash_guards/guard_heavy_command_admission.py": (
        "_CLASS_TOKENS is a tuple of executable-name stems matched against a foreign process's command "
        "line; it is never executed as an argv"
    ),
}


def _is_str(node: ast.AST, values: frozenset[str] | str) -> bool:
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    return node.value in values if isinstance(values, frozenset) else node.value == values


def pytest_argv_sites(tree: ast.AST) -> list[int]:
    """Line numbers of list/tuple literals that spell a pytest argv."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        elts = node.elts
        adjacent = any(_is_str(a, "-m") and _is_str(b, "pytest") for a, b in zip(elts, elts[1:]))
        leading = bool(elts) and _is_str(elts[0], _PYTEST_NAMES)
        if adjacent or leading:
            lines.append(node.lineno)
    return sorted(set(lines))


def imports_spawn_depth(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == _SPAWN_DEPTH for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == _SPAWN_DEPTH:
                return True
            if node.module == "coordinator_core.testing" and any(a.name == "spawn_depth" for a in node.names):
                return True
    return False


def imports_subprocess(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return True
    return False


def unrouted_sites(source: str) -> list[int]:
    """Spawn-site lines in ``source`` when it can spawn and does not import spawn_depth, else [].

    A module that never imports subprocess (classifiers, guards, string builders) cannot execute its argv.
    """
    tree = ast.parse(source)
    if not imports_subprocess(tree):
        return []
    sites = pytest_argv_sites(tree)
    return [] if imports_spawn_depth(tree) else sites


def _is_engine_module(path: Path) -> bool:
    rel = path.relative_to(CORE)
    if "__pycache__" in rel.parts or "tests" in rel.parts:
        return False
    return not (path.name.startswith("test_") or path.name == "conftest.py")


def test_unrouted_pytest_argv_is_flagged():
    bypass = "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pytest', 'x'])\n"
    assert unrouted_sites(bypass) == [2]
    assert unrouted_sites("import subprocess\nsubprocess.run(['pytest', 'x'])\n") == [2]
    assert unrouted_sites("X = ['python', '-m', 'pytest']\n") == []


def test_routed_pytest_argv_is_accepted():
    for imp in (
        "from coordinator_core.testing import spawn_depth",
        "import coordinator_core.testing.spawn_depth",
        "from coordinator_core.testing.spawn_depth import child_env",
    ):
        src = f"import subprocess, sys\n{imp}\nsubprocess.run([sys.executable, '-m', 'pytest'])\n"
        assert unrouted_sites(src) == [], imp


def test_non_pytest_argv_is_not_a_site():
    assert unrouted_sites("import sys\nx = [sys.executable, '-m', 'pip']\ny = ['git', 'status']\n") == []


def test_allowlist_entries_carry_a_reason_and_match_a_live_site():
    stale: list[str] = []
    for rel, reason in ALLOWLIST.items():
        path = CORE / rel
        if not reason.strip() or not path.is_file():
            stale.append(rel)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        if not pytest_argv_sites(tree):
            stale.append(rel)
    assert not stale, f"ALLOWLIST entries with no reason or no live pytest argv site: {stale}"


def test_every_engine_pytest_spawn_site_routes_through_spawn_depth():
    offenders: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        if not _is_engine_module(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pytest" not in text:
            continue
        rel = path.relative_to(CORE).as_posix()
        if rel in ALLOWLIST:
            continue
        try:
            lines = unrouted_sites(text)
        except SyntaxError:
            continue
        offenders.extend(f"coordinator_core/{rel}:{n}" for n in lines)
    assert not offenders, (
        "pytest argv built in a module that does not import coordinator_core.testing.spawn_depth "
        f"(wire refusal_for_spawn, selection_includes_spawner and child_env): {offenders}"
    )
