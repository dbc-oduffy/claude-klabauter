"""CI grep gate: assert no os.getcwd() / Path.cwd() and no cwd-less subprocess call
in coordinator_core C4-audited production paths (AC-5 / global-multiplex migration).

AC-5 rationale: in a single global multiplex daemon the process cwd is the daemon's
launch directory, NOT any particular repo root.  Any code that falls back to os.getcwd()
or Path.cwd() for repo discovery will silently operate against the wrong repo.
Remove all such fallbacks; pass explicit repo_root from the resolved-request envelope
(_origin_worktree) or raise ValueError / return an error envelope.

Scope: the five production files audited by C4.  Other coordinator_core/ops/ files are
checked for os.getcwd()/Path.cwd() only (not subprocess cwd= — those are addressed in
future chunks).
"""
from __future__ import annotations

import ast
import pathlib
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Files explicitly audited by C4 for BOTH os.getcwd()/Path.cwd() AND subprocess cwd=
# ---------------------------------------------------------------------------
_C4_FULL_SCOPE: list[pathlib.Path] = [
    pathlib.Path("coordinator_core/ops/coverage_gate.py"),
    pathlib.Path("coordinator_core/coverage.py"),
    pathlib.Path("coordinator_core/dag.py"),
    pathlib.Path("coordinator_core/ops/emit/sections/health.py"),
    pathlib.Path("coordinator_core/ops/emit/sections/routine_signals.py"),
]

# ---------------------------------------------------------------------------
# Implicit-cwd function pairs: (module_or_class, method) — matched as Attribute calls
# ---------------------------------------------------------------------------
_IMPLICIT_CWD_ATTRS: frozenset[tuple[str, str]] = frozenset({
    ("os", "getcwd"),
    ("Path", "cwd"),
})

# subprocess call names that must carry an explicit cwd= keyword
_SUBPROCESS_NAMES: frozenset[str] = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
})


def _repo_root() -> pathlib.Path:
    """Return the absolute path to the repository root (two dirs up from this file)."""
    return pathlib.Path(__file__).resolve().parent.parent.parent


def _resolve(rel: pathlib.Path) -> pathlib.Path:
    return _repo_root() / rel


# ---------------------------------------------------------------------------
# AST analysis helpers
# ---------------------------------------------------------------------------

def _find_implicit_cwd_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, call_str) for every os.getcwd() / Path.cwd() call in tree."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in _IMPLICIT_CWD_ATTRS:
                hits.append((node.lineno, f"{func.value.id}.{func.attr}()"))
    return hits


def _find_subprocess_without_cwd(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, call_str) for subprocess.X(...) calls missing explicit cwd=."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in _SUBPROCESS_NAMES):
            has_cwd = any(kw.arg == "cwd" for kw in node.keywords)
            if not has_cwd:
                hits.append((node.lineno, f"subprocess.{func.attr}(...)"))
    return hits


# ---------------------------------------------------------------------------
# Parametrized tests — full C4 scope (both checks)
# ---------------------------------------------------------------------------

def _c4_full_ids(paths: list[pathlib.Path]) -> list[str]:
    root = _repo_root()
    return [str(p.relative_to(root)) if p.is_absolute() else str(p) for p in paths]


def _c4_full_paths() -> list[pathlib.Path]:
    resolved = [_resolve(p) for p in _C4_FULL_SCOPE]
    missing = [p for p in resolved if not p.is_file()]
    assert not missing, (
        "AC-5 audited scope shrank silently — file(s) missing on disk: "
        + ", ".join(str(p) for p in missing)
        + f". A renamed/deleted _C4_FULL_SCOPE entry must be removed from that list "
        "explicitly (a visible, reviewable edit), not silently filtered out here."
    )
    return resolved


@pytest.mark.parametrize("path", _c4_full_paths(), ids=lambda p: str(p.relative_to(_repo_root())))
def test_no_implicit_cwd_in_c4_files(path: pathlib.Path) -> None:
    """AC-5: no os.getcwd() / Path.cwd() in C4-audited production files."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    hits = _find_implicit_cwd_calls(tree)
    assert not hits, (
        f"AC-5 violation — os.getcwd()/Path.cwd() found in {path}:\n"
        + "\n".join(f"  line {ln}: {call}" for ln, call in hits)
        + "\nDaemon cwd is not a valid repo root — pass explicit repo_root."
    )


@pytest.mark.parametrize("path", _c4_full_paths(), ids=lambda p: str(p.relative_to(_repo_root())))
def test_no_cwd_less_subprocess_in_c4_files(path: pathlib.Path) -> None:
    """AC-5: no subprocess.run/Popen/call without explicit cwd= in C4-audited files."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    hits = _find_subprocess_without_cwd(tree)
    assert not hits, (
        f"AC-5 violation — subprocess call without cwd= found in {path}:\n"
        + "\n".join(f"  line {ln}: {call}" for ln, call in hits)
        + "\nAll subprocess calls must carry an explicit cwd= in C4-audited files."
    )
