"""
coordinator_core.hooks.tests.test_eager_hook_modules_covers_every_register_op --
completeness guard: every `coordinator_core/hooks/*.py` module that declares an
op via `@register_op(...)` must be reachable through `_EAGER_HOOK_MODULES`
(coordinator_core/hooks/__init__.py), or the op ships present-but-dead --
correctly decorated, never registered, and `coordinator-invoke` cannot resolve
it.

WHY THIS GUARD DOES NOT IMPORT THE MODULES UNDER TEST (negative spec):
The RELAY tests in test_cater_subagent_start_relay.py were green throughout the
window in which `hooks.cater_subagent_start` shipped inert, because that file's
own `from coordinator_core.hooks.cater_subagent_start import ...` statement
registers the op as an import side effect before any assertion runs -- the
decorator fires on import regardless of whether the eager list names the module.
So the op was dead on every real path while its own test suite read green.

Corrected 2026-08-21: an earlier revision of this docstring attributed that
green-over-a-dead-op behaviour to
`test_module_is_eagerly_imported_by_the_hooks_package` specifically. That is
false in two checkable ways, and it is recorded here rather than quietly
deleted because a negative spec that misstates its own evidence is worse than
none. (1) That test asserted LIST MEMBERSHIP in `_EAGER_HOOK_MODULES`, not
registry state; an import side effect populates `ipc._REGISTRY` and cannot
append to a static list literal, so no import could have made it pass while the
entry was absent. (2) It never ran against the broken state at all -- `git log
-S` puts its introduction in `367b15a88`, the same commit that added the eager
entry. The general principle below is sound and is why this module parses
source; the specific counterexample it was hung on was not.

Any guard that imports (directly, or transitively via another
already-imported test module) the modules it is checking can never observe
the "declared but unreachable" state; it can only observe "was imported by
something, at some point, in this process." This module therefore parses
`coordinator_core/hooks/*.py` as TEXT with `ast` -- no `importlib`,
no `__import__`, no `subprocess` -- and reads `_EAGER_HOOK_MODULES` as a
plain Python list literal out of `__init__.py`'s own source, never by
importing that package either.

Spec backlink: this dispatch's task (completeness guard for the hooks eager
list), sibling of the ops-package guard the same incident motivated.
"""

from __future__ import annotations

import ast
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent
_INIT_PATH = _HOOKS_DIR / "__init__.py"

#: Filenames under coordinator_core/hooks/ that are package plumbing, not
#: op-handler modules, and are therefore never expected to appear in
#: _EAGER_HOOK_MODULES.
_NON_OP_MODULE_NAMES = {"__init__.py"}


def _load_eager_hook_modules() -> list[str]:
    """Extract `_EAGER_HOOK_MODULES`'s string entries straight from the
    `__init__.py` source via `ast`, without importing the package (importing
    `coordinator_core.hooks` would itself run `_eager_import_all()` and
    register every op as a side effect -- exactly the trap this guard exists
    to avoid)."""
    tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"), filename=str(_INIT_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            # `_EAGER_HOOK_MODULES: list[str] = [...]` -- the actual shape in
            # __init__.py, distinct from a plain ast.Assign.
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_EAGER_HOOK_MODULES" for t in targets):
            continue
        if not isinstance(value, ast.List):
            continue
        entries: list[str] = []
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                entries.append(elt.value)
        return entries
    raise AssertionError(
        f"_EAGER_HOOK_MODULES not found as a module-level list literal in {_INIT_PATH} -- "
        "this guard's ast-based extraction relies on that exact shape."
    )


def _module_level_string_constants(tree: ast.Module) -> dict[str, str]:
    """Map NAME -> value for every module-level `NAME = "literal"` assignment,
    used to resolve `@register_op(OP_NAME)`-style decorators where the op name
    is a constant rather than an inline literal."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants[node.targets[0].id] = node.value.value
    return constants


def _register_op_calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    calls = []
    for dec in func.decorator_list:
        call = dec
        if isinstance(call, ast.Call):
            callee = call.func
            if isinstance(callee, ast.Name) and callee.id == "register_op":
                calls.append(call)
            elif isinstance(callee, ast.Attribute) and callee.attr == "register_op":
                calls.append(call)
    return calls


def _resolve_op_name(call: ast.Call, constants: dict[str, str]) -> str | None:
    """Resolve the op-name argument of a `register_op(...)` call to a plain
    string, either an inline literal or a module-level constant reference.
    Returns None if the argument's value cannot be resolved statically."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name) and arg.id in constants:
        return constants[arg.id]
    return None


def _modules_declaring_register_op() -> dict[str, list[str | None]]:
    """Walk coordinator_core/hooks/*.py on the filesystem and return
    {dotted module path: [resolved op name or None, ...]} for every module
    with at least one module-level `@register_op(...)`-decorated function.

    A module contributes a `None` entry (rather than being omitted) when it
    declares a register_op call whose name argument cannot be resolved
    statically -- the module still MUST be in the eager list even though this
    guard cannot name the op it registers.
    """
    declared: dict[str, list[str | None]] = {}
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        if path.name in _NON_OP_MODULE_NAMES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _module_level_string_constants(tree)
        op_names: list[str | None] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in _register_op_calls(node):
                op_names.append(_resolve_op_name(call, constants))
        if op_names:
            module_path = "coordinator_core.hooks." + path.stem
            declared[module_path] = op_names
    return declared


def _missing_from_eager_list(
    declared: dict[str, list[str | None]], eager_list: list[str]
) -> dict[str, list[str | None]]:
    eager_set = set(eager_list)
    return {mod: names for mod, names in declared.items() if mod not in eager_set}


def _stale_eager_entries(declared: dict[str, list[str | None]], eager_list: list[str]) -> list[str]:
    """Eager-list entries whose module no longer exists on disk at all (the
    file is gone), OR whose file exists but declares no register_op --
    either way the entry is stale and should be caught loudly rather than
    silently importing dead weight forever."""
    declared_set = set(declared)
    stale = []
    for module_path in eager_list:
        stem = module_path.rsplit(".", 1)[-1]
        file_path = _HOOKS_DIR / f"{stem}.py"
        if not file_path.exists():
            stale.append(f"{module_path} (file does not exist: {file_path})")
        elif module_path not in declared_set:
            stale.append(f"{module_path} (file exists but declares no @register_op)")
    return stale


def test_every_register_op_module_is_in_the_eager_hook_list() -> None:
    declared = _modules_declaring_register_op()
    eager_list = _load_eager_hook_modules()
    missing = _missing_from_eager_list(declared, eager_list)

    assert not missing, (
        "The following coordinator_core/hooks module(s) declare @register_op "
        "but are absent from _EAGER_HOOK_MODULES in coordinator_core/hooks/__init__.py: "
        + ", ".join(sorted(missing))
        + ". A decorator alone does not register an op -- importing "
        "coordinator_core.hooks never reaches these modules, so their op(s) "
        "ship present-but-dead and coordinator-invoke cannot resolve them "
        "(registry MISS at dispatch time). Add the dotted module path(s) to "
        "_EAGER_HOOK_MODULES."
    )


def test_every_register_op_module_has_a_statically_resolvable_op_name() -> None:
    """Not a hard requirement of the eager-list guard above (which checks
    module membership regardless of name resolvability), but a sanity check
    that this guard's own resolution logic is keeping up with the modules it
    covers -- an unresolvable name means a human should double check the
    module by eye, since this guard falls back to module-only coverage for it."""
    declared = _modules_declaring_register_op()
    unresolvable = {mod: names for mod, names in declared.items() if None in names}
    assert not unresolvable, (
        "The following module(s) declare @register_op with a name argument this "
        "guard could not resolve statically (module-only coverage still enforced "
        "above, but the op name itself is unverified): " + ", ".join(sorted(unresolvable))
    )


def test_eager_hook_list_has_no_stale_entries() -> None:
    declared = _modules_declaring_register_op()
    eager_list = _load_eager_hook_modules()
    stale = _stale_eager_entries(declared, eager_list)

    assert not stale, (
        "The following _EAGER_HOOK_MODULES entries in coordinator_core/hooks/__init__.py "
        "are stale (no matching file, or the file declares no @register_op): "
        + ", ".join(stale)
    )


def test_declared_and_eager_counts_agree_exactly() -> None:
    """Redundant with the two directional checks above by construction, but
    asserted directly as the single number this guard's docstring and the
    dispatching task both cite (20 declaring modules, 20 eager entries) --
    a cheap tripwire if the two checks above are ever weakened independently."""
    declared = _modules_declaring_register_op()
    eager_list = _load_eager_hook_modules()
    assert len(declared) == len(eager_list), (
        f"{len(declared)} coordinator_core/hooks module(s) declare @register_op "
        f"but _EAGER_HOOK_MODULES has {len(eager_list)} entries -- these must "
        "agree exactly (see the two directional checks in this file for which "
        "side is missing what)."
    )


# ---------------------------------------------------------------------------
# Proof the guard bites: exercise the comparison function directly with a
# synthetic missing entry, rather than mutating the real eager list.
# ---------------------------------------------------------------------------

def test_missing_from_eager_list_detects_a_synthetic_gap() -> None:
    declared = {
        "coordinator_core.hooks.real_module": ["hooks.real_module"],
        "coordinator_core.hooks.orphaned_module": ["hooks.orphaned_module"],
    }
    eager_list = ["coordinator_core.hooks.real_module"]  # orphaned_module deliberately omitted

    missing = _missing_from_eager_list(declared, eager_list)

    assert missing == {"coordinator_core.hooks.orphaned_module": ["hooks.orphaned_module"]}


def test_stale_eager_entries_detects_a_synthetic_ghost_entry() -> None:
    # track_touched_files.py is a real, on-disk hooks module that genuinely
    # declares @register_op -- used here as the non-stale control so only the
    # fictional second entry is flagged.
    declared = {"coordinator_core.hooks.track_touched_files": ["hooks.track_touched_files"]}
    eager_list = [
        "coordinator_core.hooks.track_touched_files",
        "coordinator_core.hooks.this_module_does_not_exist_anywhere",
    ]

    stale = _stale_eager_entries(declared, eager_list)

    assert len(stale) == 1
    assert "this_module_does_not_exist_anywhere" in stale[0]
