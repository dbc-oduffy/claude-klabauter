"""
coordinator_core.merge_assemble.tests.test_no_interpreter_spawn — C3 AC6, a
static AST regression guard on the CONVERGED handlers' source shape only.

Purpose: C2 moved four of `merge_assemble.apply`'s eight `_CLI_DISPATCH`
handlers in-process (`_dispatch_merge_recovery_and_tag_cut`,
`_dispatch_portability_sweep`, `_dispatch_check_no_illegal_paths`,
`_dispatch_tier_u_grant` — the module's own decision comment above
`_CLI_DISPATCH` names all four). This test asserts, by walking each
converged handler's own `ast.FunctionDef` body, that no `sys.executable`
or `subprocess` reference remains inside it — a future edit that
re-introduces a spawn into one of these four bodies fails this test.

Follows the existing spawn-guard idiom (source-shape AST assertion, not a
runtime trace) established in
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`.

NEGATIVE SPEC, stated per the dispatch brief that authored this file: this
is a cheap regression guard on source SHAPE. It CANNOT see an interpreter
started transitively — e.g. a converged script's own
`subprocess.run([sys.executable, ...])` reachable through
`ceremony_common.cli_dispatch.invoke_cli_main`, or a future helper import
that spawns one — so it is not, by itself, the runtime "zero interpreter
processes" claim. That claim is AC7, discharged separately via
`coordinator_core.benchmarks.process_time.batched_process_time_ms` over the
real published-engine invocation (state/lessons/2026-08-20-a-process-
boundary-ac-cannot-be-discharged-by-pytest.md), not by this pytest module.
The guard now also flags any `import subprocess` / `from subprocess import
...` inside a converged handler body, closing the import-aliasing gap
(e.g. `from subprocess import run as _r`) that the dotted-path and
bare-name checks alone could not see — a materially easier evasion than
the transitive-spawn gap above, so it is named here rather than left
implicit.

The three EXCLUDED handlers (`_dispatch_merge_gate_and_pr`,
`_dispatch_merge_release_notes_derive`, `_dispatch_orphan_branch_sweep`) and
`_dispatch_node_ceremony_gate` (a genuine external-program spawn, `node
--test`) are deliberately NOT scanned here — each still spawns by design
(each handler's own docstring in `merge_assemble/apply.py` records why),
and asserting a subprocess-free body on them would be a false claim, not a
regression guard.

No process spawn, no git — fast tier.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from coordinator_core.merge_assemble import apply as ma_apply

#: The four handlers C2 converted in-process. Names come straight from the
#: module's own `_CLI_DISPATCH` table and its decision comment above it —
#: not re-derived here.
_CONVERGED_HANDLER_NAMES = (
    "_dispatch_merge_recovery_and_tag_cut",
    "_dispatch_portability_sweep",
    "_dispatch_check_no_illegal_paths",
    "_dispatch_tier_u_grant",
)

#: Handlers that still spawn by design (each with a docstring naming why it
#: was excluded from C2's conversion) or that spawn a genuine external
#: program with no import path. Listed here only so a future edit that
#: mislabels one as "converged" is caught: this set and
#: `_CONVERGED_HANDLER_NAMES` must partition `_CLI_DISPATCH`'s handler set.
_STILL_SPAWNING_HANDLER_NAMES = (
    "_dispatch_node_ceremony_gate",
    "_dispatch_merge_gate_and_pr",
    "_dispatch_merge_release_notes_derive",
    "_dispatch_orphan_branch_sweep",
)


def _names_referenced(node: ast.AST) -> set[str]:
    """Every bare `ast.Name`/`ast.Attribute` root identifier referenced
    anywhere inside `node`, e.g. `subprocess.run(...)` yields `subprocess`
    and a bare `sys.executable` yields `sys`."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
    return found


def _attribute_dotted_paths(node: ast.AST) -> set[str]:
    """Every `a.b` / `a.b.c` dotted attribute-access path inside `node`,
    rendered as a `.`-joined string (e.g. `sys.executable`,
    `subprocess.run`) so a specific banned reference can be matched exactly
    rather than by a bare root name, which would also flag an unrelated
    local named `sys` or `subprocess`."""
    paths: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute):
            continue
        parts: list[str] = [child.attr]
        cur = child.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            paths.add(".".join(reversed(parts)))
    return paths


def _imports_subprocess(node: ast.AST) -> bool:
    """True if `node` contains an `import subprocess` (bare or aliased) or a
    `from subprocess import ...` (aliased or not) anywhere in its body —
    closes the aliasing gap where `from subprocess import run as _r` evades
    both the dotted-path check (no `subprocess.` attribute access) and the
    bare-name check (no reference to the literal name `subprocess`)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            if any(alias.name == "subprocess" for alias in child.names):
                return True
        elif isinstance(child, ast.ImportFrom):
            if child.module == "subprocess":
                return True
    return False


def _function_defs_by_name(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


@pytest.fixture(scope="module")
def _apply_tree() -> ast.Module:
    source = inspect.getsource(ma_apply)
    return ast.parse(source, filename=ma_apply.__file__)


@pytest.fixture(scope="module")
def _handler_defs(_apply_tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return _function_defs_by_name(_apply_tree)


def test_converged_and_spawning_sets_partition_cli_dispatch_table(
    _handler_defs: dict[str, ast.FunctionDef],
) -> None:
    """`_CONVERGED_HANDLER_NAMES` plus `_STILL_SPAWNING_HANDLER_NAMES` must
    equal exactly the handler set `_CLI_DISPATCH` actually dispatches — a
    tripwire against this file drifting from `apply.py`'s own table
    (e.g. a ninth handler added to the table but not classified here)."""
    dispatch_table_names = {fn.__name__ for fn in ma_apply._CLI_DISPATCH.values()}
    classified_names = set(_CONVERGED_HANDLER_NAMES) | set(_STILL_SPAWNING_HANDLER_NAMES)
    assert classified_names == dispatch_table_names
    for name in classified_names:
        assert name in _handler_defs, f"{name} not found as a module-level def in apply.py"


@pytest.mark.parametrize("handler_name", _CONVERGED_HANDLER_NAMES)
def test_converged_handler_body_has_no_interpreter_spawn_reference(
    handler_name: str, _handler_defs: dict[str, ast.FunctionDef]
) -> None:
    """Static AST assertion: none of `sys.executable`, `subprocess.run`, a
    bare `subprocess` reference, or `_run_py_script` (the module's own
    subprocess-spawning helper) appears anywhere in this converged
    handler's body."""
    fn = _handler_defs[handler_name]
    root_names = _names_referenced(fn)
    dotted_paths = _attribute_dotted_paths(fn)

    assert "subprocess" not in root_names, (
        f"{handler_name}: bare `subprocess` reference found in a converged "
        "(in-process) handler body — this handler is supposed to never spawn"
    )
    banned_dotted = {p for p in dotted_paths if p == "sys.executable" or p.startswith("subprocess.")}
    assert not banned_dotted, (
        f"{handler_name}: found {sorted(banned_dotted)} — a converged "
        "(in-process) handler body must not reference sys.executable or "
        "any subprocess.* call"
    )
    called_names = {
        child.func.id
        for child in ast.walk(fn)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }
    assert "_run_py_script" not in called_names, (
        f"{handler_name}: calls _run_py_script (the module's own "
        "interpreter-spawning helper) — this handler is supposed to be "
        "converged in-process"
    )
    assert not _imports_subprocess(fn), (
        f"{handler_name}: imports `subprocess` (directly or via `from "
        "subprocess import ...`, aliased or not) inside a converged "
        "(in-process) handler body — this evades the dotted-path and "
        "bare-name checks above and is not a legitimate way to reintroduce "
        "a spawn"
    )


def test_still_spawning_handlers_are_unchanged_by_this_guard(
    _handler_defs: dict[str, ast.FunctionDef],
) -> None:
    """Sanity check on the guard itself: the three EXCLUDED handlers plus
    the genuine external-program spawn still reference either
    `_run_py_script` or `subprocess` directly, so this test module is
    exercising a real distinction rather than a guard that would pass on
    everything regardless of source shape."""
    for handler_name in _STILL_SPAWNING_HANDLER_NAMES:
        fn = _handler_defs[handler_name]
        root_names = _names_referenced(fn)
        called_names = {
            child.func.id
            for child in ast.walk(fn)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        assert "subprocess" in root_names or "_run_py_script" in called_names, (
            f"{handler_name}: expected to still spawn (via subprocess or "
            "_run_py_script) — if this now passes clean, the handler was "
            "converged and belongs in _CONVERGED_HANDLER_NAMES instead"
        )
