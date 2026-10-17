"""Guard: every non-test `run_token.consume` / `run_token.discard` call site is named, with the
ended-run condition under which it removes a record. The condition text is documentation; only the
site and its call count are enforced.

An unlisted site is a new way to strand a still-running run's token; add it to `_ALLOWED` with its
condition, or remove the call. The walk is in-process `ast`; no spawn.
"""

from __future__ import annotations

import ast
from pathlib import Path

import coordinator_core

_PKG = Path(coordinator_core.__file__).resolve().parent
_REMOVERS = {"consume", "discard"}
_MODULE = "run_token"

# (repo-relative file, enclosing function) -> (call count, ended-run condition)
_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("warp_live.py", "_record_live"): (
        1, "launch this mint submitted has terminal evidence (run ended)"),
    ("hooks/submit_engine_ask.py", "evaluate"): (
        2, "unlaunched run refused at submission, or the emit of this call's own run failed"),
    ("ops/dispatch_emit/ask_stage.py", "_refuse_live_write_overlap"): (
        1, "write-overlap refusal ending this run"),
    ("ops/dispatch_emit/terminal_commit.py", "_settle_token"): (
        1, "commit landed (sha present)"),
    ("ops/dispatch_emit/grind_terminal_commit.py", "commit_grind_rows"): (
        1, "grind commit landed"),
    ("ops/dispatch_emit/roadmap_fanout_commit.py", "commit_fanout_record"): (
        1, "fanout commit landed"),
    ("ops/dispatch_emit/release_run_token.py", "_handler"): (
        1, "sanctioned peer release of an unclaimed record"),
}


def _bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to the run_token module, names bound to its consume/discard functions)."""
    modules: set[str] = set()
    funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] == _MODULE:
                    modules.add(a.asname or a.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == _MODULE:
                    modules.add(a.asname or a.name)
                elif (node.module or "").split(".")[-1] == _MODULE and a.name in _REMOVERS:
                    funcs.add(a.asname or a.name)
    return modules, funcs


def _sites(path: Path) -> list[str]:
    """Enclosing-function name of each remover call in `path`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    modules, funcs = _bindings(tree)
    if not modules and not funcs:
        return []
    found: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            if isinstance(child, ast.Call):
                f = child.func
                if (isinstance(f, ast.Attribute) and f.attr in _REMOVERS
                        and isinstance(f.value, ast.Name) and f.value.id in modules):
                    found.append(scope)
                elif isinstance(f, ast.Name) and f.id in funcs:
                    found.append(scope)
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _scan() -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        parts = rel.split("/")
        if "tests" in parts or parts[-1].startswith("test_") or rel == "ops/dispatch_emit/run_token.py":
            continue
        for scope in _sites(path):
            counts[(rel, scope)] = counts.get((rel, scope), 0) + 1
    return counts


def test_every_run_token_remover_is_allowlisted_with_its_condition():
    found = _scan()
    unlisted = {k: n for k, n in found.items() if k not in _ALLOWED}
    assert not unlisted, f"run_token remover not in _ALLOWED (name its ended-run condition): {unlisted}"
    drift = {k: (found.get(k, 0), n) for k, (n, _) in _ALLOWED.items() if found.get(k, 0) != n}
    assert not drift, f"allowlist count drift (found, expected): {drift}"
    assert all(cond.strip() for _, cond in _ALLOWED.values())


def test_walk_resolves_aliased_and_from_imports(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(
        "from x import run_token as _rt\n"
        "from x.run_token import discard as d\n"
        "def a(r):\n    _rt.consume(r, 'i')\n"
        "def b(r):\n    d(r, 'i')\n"
        "def c(r):\n    other.consume(r)\n",
        encoding="utf-8",
    )
    assert sorted(_sites(src)) == ["a", "b"]
