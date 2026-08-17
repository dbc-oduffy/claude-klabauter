"""coordinator_core.bash_guards.tests.test_hot_path_subprocess_timeouts --
static pin: every ``subprocess.run``/``Popen``/``check_output``/``call``
reachable, at IMPORT time, from ``dispatch.evaluate_payload_json`` must pass
an explicit ``timeout=`` keyword.

Why AST, not grep: a regex over source text is defeated by formatting (a
multiline call whose ``timeout=`` keyword lands three lines below the
matched ``subprocess.run(``, or a call split across a helper wrapper). This
walks the real syntax tree of every module the guard chain actually
imports, so reformatting or reordering keyword arguments cannot hide a
missing timeout from this test. It also tracks module-level
``from subprocess import run as r``-shaped bindings (including aliases,
Review: code-reviewer F3, P2) and flags bare-name calls through them, so
aliasing the import cannot hide a missing timeout either -- see
``_subprocess_name_bindings``. Function-local (not module-level) aliasing
imports are the one remaining gap; see that helper's own docstring.

Incident this pins (2026-08-05 PM ruling: "spawn sites need to have
timeouts, that can save a machine from getting degraded-stuck"): at least
two spawn sites on this exact chain had no timeout, or an inconsistent one,
found only because a human went looking after a Windows machine was
degraded by guard-chain latency:
  - ``subagent_sandbox.engine._resolve_git_root_uncached`` -- NO timeout at
    all, on a function every dispatch calls.
  - ``bash_guards._write_bump_marker.resolve_gitdir`` -- ``timeout=10``,
    against the house value (``dispatch_checks._run_git``) of ``2.0``.
Sibling fixes made in the same pass: ``write_guards.block_subagent_plan_
body_write._resolve_git_root``/``_resolve_git_dir`` (no timeout) and
``bash_guards._branch_set._GIT_TIMEOUT`` (``15``, no stated reason).

Scope -- MODULE-LEVEL imports only (a module's ``tree.body``, not every
``ast.Import``/``ImportFrom`` found anywhere via ``ast.walk``): that is the
import-time graph that actually executes when ``dispatch`` is imported and
becomes callable, which is what "reachable from evaluate_payload_json"
means for a spawn-timeout audit. A few guard modules also carry FUNCTION-
LOCAL imports for unrelated (non-subprocess) call-time resolution (e.g.
``commit_tripwires._resolve_doe_coordinator_root``'s deferred
``coordinator_core.ops.coordinator_doe_root`` import) -- those are a
separate lazy-reachability question this test does not attempt to answer,
and none of the actual subprocess call sites found during this hardening
pass were hidden behind one. ``if TYPE_CHECKING:`` blocks are skipped
(never execute at runtime).

Pure static analysis -- no clock, no flake surface, no real subprocess
spawned. Belongs in the fast tier (unmarked).

Spec backlink: 2026-08-05 PreToolUse(Bash) hot-path hardening dispatch
(Deliverable 1).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set, Tuple

import pytest

_ROOT_MODULE = "coordinator_core.bash_guards.dispatch"
_REPO_ROOT = Path(__file__).resolve().parents[3]

_SUBPROCESS_SPAWN_ATTRS = {"run", "Popen", "check_output", "call"}

#: Explicit, NAMED exemption list -- a call landing here is a visible
#: decision (module, function, reason), never a silent omission.
#: Shape: {(module_dotted_name, enclosing_function_name): "reason"}.
_EXEMPT: Dict[Tuple[str, str], str] = {
    ("coordinator_core.win_portability", "run_forwarding"): (
        "Not on the hot path. This sweep's reachability is MODULE-level, not "
        "call-graph-level: `win_portability` is imported by the chain for its "
        "console-suppression primitives, which is what drags every function in "
        "the module into scope. `run_forwarding` itself has no production "
        "caller anywhere in coordinator_core (only a test), so no dispatch path "
        "reaches this spawn. It is also a pass-through wrapper that forwards "
        "**kwargs verbatim, so a timeout belongs to the CALLER: hard-coding one "
        "here would silently cap every future caller's budget, which is a "
        "different defect rather than a fix. CONDITION ON THIS EXEMPTION -- if a "
        "hot-path caller is ever added, it must pass timeout= explicitly and "
        "this entry must be re-examined rather than inherited."
    ),
}


def _module_file(mod_name: str) -> "Path | None":
    parts = mod_name.split(".")
    as_module = _REPO_ROOT.joinpath(*parts).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = _REPO_ROOT.joinpath(*parts, "__init__.py")
    if as_package.is_file():
        return as_package
    return None


def _is_type_checking_guard(node: ast.stmt) -> bool:
    """True for an ``if TYPE_CHECKING:`` (or ``typing.TYPE_CHECKING``)
    module-level guard -- its body never executes at runtime, so imports
    inside it are not part of the real import graph."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_level_imports(tree: ast.Module) -> Set[str]:
    """Every ``coordinator_core.*`` module name this file's TOP-LEVEL
    statements import (module-level only -- see docstring "Scope")."""
    found: Set[str] = set()
    for node in tree.body:
        if _is_type_checking_guard(node):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("coordinator_core"):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # No relative imports observed on this chain today; skip
                # rather than mis-resolve one if a future edit adds one.
                continue
            if not node.module or not node.module.startswith("coordinator_core"):
                continue
            found.add(node.module)
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                if _module_file(candidate) is not None:
                    found.add(candidate)
    return found


def _reachable_modules(root: str) -> Set[str]:
    seen: Set[str] = set()
    frontier = [root]
    while frontier:
        mod = frontier.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_file(mod)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in _module_level_imports(tree):
            if imported not in seen:
                frontier.append(imported)
    return seen


def _enclosing_function_name(func_stack: list) -> str:
    return func_stack[-1] if func_stack else "<module>"


def _subprocess_name_bindings(tree: ast.Module) -> Dict[str, str]:
    """Module-level ``from subprocess import run as r``-shaped bindings ->
    the real attribute name (``r`` -> ``run``). Review: code-reviewer F3,
    P2 -- the AST pin was originally blind to this aliased-name-call idiom
    (``ast.Name``, not the ``ast.Attribute`` shape the visitor matched),
    despite the identity-hiding technique being live prose elsewhere in
    this same package (``block_subagent_commit.py``). Module-level only,
    mirroring ``_module_level_imports``'s own "Scope" contract -- a
    function-local aliasing import is a separate lazy-reachability
    question this pin does not attempt to answer."""
    bindings: Dict[str, str] = {}
    for node in tree.body:
        if _is_type_checking_guard(node):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SUBPROCESS_SPAWN_ATTRS:
                    bindings[alias.asname or alias.name] = alias.name
    return bindings


def _find_unbudgeted_spawns(mod_name: str) -> list:
    path = _module_file(mod_name)
    if path is None:
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    name_bindings = _subprocess_name_bindings(tree)

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            is_attr_spawn = (
                isinstance(func, ast.Attribute)
                and func.attr in _SUBPROCESS_SPAWN_ATTRS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            is_aliased_name_spawn = (
                isinstance(func, ast.Name) and func.id in name_bindings
            )
            if is_attr_spawn or is_aliased_name_spawn:
                enclosing = _enclosing_function_name(self.func_stack)
                has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                if not has_timeout and (mod_name, enclosing) not in _EXEMPT:
                    offenders.append((mod_name, enclosing, node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return offenders


def test_every_reachable_subprocess_spawn_has_explicit_timeout():
    modules = _reachable_modules(_ROOT_MODULE)
    assert "coordinator_core.subagent_sandbox.engine" in modules, (
        "sanity check: the known _resolve_git_root_uncached spawn site must "
        "be part of the discovered import graph, or this test is silently "
        "checking nothing"
    )

    offenders = []
    for mod_name in sorted(modules):
        offenders.extend(_find_unbudgeted_spawns(mod_name))

    assert not offenders, (
        "subprocess call(s) reachable from dispatch.evaluate_payload_json "
        "with no explicit timeout= keyword (add one, or add a NAMED entry "
        "to _EXEMPT with a reason): "
        + ", ".join(f"{m}::{fn} (line {ln})" for m, fn, ln in offenders)
    )


def test_pin_is_red_against_a_deliberately_timeout_less_call(tmp_path):
    """Negative control (per dispatch brief step 3): prove the pin actually
    fires, rather than passing vacuously. A synthetic module with a bare
    ``subprocess.run(...)`` (no ``timeout=``) must be flagged by the same
    detector this test module runs against the real chain."""
    offending_src = (
        "import subprocess\n\n"
        "def _spawn():\n"
        "    return subprocess.run(['git', 'status'])\n"
    )
    tree = ast.parse(offending_src, filename="<synthetic>")
    offenders = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _SUBPROCESS_SPAWN_ATTRS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                if not has_timeout:
                    offenders.append((_enclosing_function_name(self.func_stack), node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert offenders == [("_spawn", 4)]


def test_aliased_subprocess_import_call_without_timeout_is_flagged(tmp_path, monkeypatch):
    """Review: code-reviewer F3, P2 -- the visitor must not be blind to
    ``from subprocess import run as r; r(...)`` (an ``ast.Name`` call, not
    the ``ast.Attribute`` shape the original detector matched). Writes a
    real module under a throwaway `coordinator_core` package layout so
    `_module_file`/`_find_unbudgeted_spawns` exercise the real resolution
    path rather than a synthetic in-memory tree."""
    pkg_root = tmp_path / "coordinator_core" / "_synthetic_alias_probe"
    pkg_root.mkdir(parents=True)
    mod_path = pkg_root.with_suffix(".py")
    mod_path.write_text(
        "from subprocess import run as r\n\n"
        "def _spawn():\n"
        "    return r(['git', 'status'])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "coordinator_core.bash_guards.tests.test_hot_path_subprocess_timeouts._REPO_ROOT",
        tmp_path,
    )
    offenders = _find_unbudgeted_spawns("coordinator_core._synthetic_alias_probe")
    assert offenders == [
        ("coordinator_core._synthetic_alias_probe", "_spawn", 4)
    ]


def test_reachable_modules_includes_every_named_fix_site():
    """Sanity leg: every module this dispatch actually patched must be part
    of the discovered graph, or the sweep above proves nothing about them."""
    modules = _reachable_modules(_ROOT_MODULE)
    for expected in (
        "coordinator_core.subagent_sandbox.engine",
        "coordinator_core.write_guards.block_subagent_plan_body_write",
        "coordinator_core.bash_guards._write_bump_marker",
        "coordinator_core.bash_guards._branch_set",
        "coordinator_core.bash_guards.dispatch_checks",
        "coordinator_core.bash_guards.commit_tripwires",
    ):
        assert expected in modules, f"{expected} missing from the discovered import graph"


def test_run_forwarding_exemption_premise_still_holds():
    """Pin the fact the `run_forwarding` exemption rests on.

    That entry is justified by "no production caller reaches this spawn". An
    exemption is only as good as its premise, and this one is a fact about the
    tree that a future commit can silently falsify — someone wiring
    `run_forwarding` into a guard would inherit a timeout exemption nobody
    re-examined, which is exactly the silent-omission shape `_EXEMPT` exists to
    prevent.

    NEGATIVE SPEC: this does not assert `run_forwarding` is unused forever. It
    asserts that if it gains a NON-TEST caller, someone is forced back to the
    exemption to re-justify it.
    """
    offenders = []
    for path in _REPO_ROOT.joinpath("coordinator_core").rglob("*.py"):
        name = path.name
        if name.startswith("test_") or "/tests/" in path.as_posix():
            continue
        if path.as_posix().endswith("coordinator_core/win_portability.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "run_forwarding(" in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))

    assert not offenders, (
        "run_forwarding gained a production caller, so the _EXEMPT entry's premise "
        f"('no production caller reaches this spawn') no longer holds: {offenders}. "
        "Either pass timeout= at that call site, or rewrite the exemption reason to "
        "match the new reality — do not leave it inherited."
    )
