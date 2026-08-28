"""
coordinator_core.ops.tests.test_eager_op_modules_covers_every_register_op --
completeness guard: every module under `coordinator_core/` that declares an op
via `@register_op(...)` must be reachable through `_EAGER_OP_MODULES`
(coordinator_core/ops/__init__.py) -- membership in `OP_MODULE_MAP`
(coordinator_core/ops/_registry_map.py) does NOT confer reachability -- or the
op ships present-but-dead: correctly decorated, possibly even mapped, never
actually registered, and `coordinator-invoke` cannot resolve it.

Sibling of coordinator_core/hooks/tests/test_eager_hook_modules_covers_every_register_op.py,
the guard the same incident motivated (see that module's docstring for the
full incident writeup). This module adapts the same technique to the ops
package's wider, nested tree.

CORRECTED RULE (was: union of `_EAGER_OP_MODULES` OR `OP_MODULE_MAP`). An
earlier revision of this guard treated `OP_MODULE_MAP` presence as an
alternate reachability path, on the theory that `ipc.py`'s registry-miss
lazy import via `OP_MODULE_MAP` would reach the module even absent from the
eager list. That was wrong, and wrong in the exact direction that defeats
this guard's purpose: `sizing.read_object_fields` was present in
`OP_MODULE_MAP`, decorated with `@register_op`, and ABSENT from
`_EAGER_OP_MODULES` -- i.e. genuinely dead, since `import coordinator_core.ops`
never registers it -- and the union rule would have called it reachable and
passed. The authoritative rule is stated in
`coordinator_core/authz/registration_quad.py`'s module docstring
(`Design` / `Fifth surface` sections): every `OP_MODULE_MAP` entry's module
must ALSO be reachable through `_EAGER_OP_MODULES` -- `OP_MODULE_MAP`
membership is not itself a registration surface an op can rely on in place of
the eager list, it is a lazy-import optimization keyed off a module already
expected to be eager-reachable. This guard's positive check is now
`_EAGER_OP_MODULES` membership alone.

Three real instances of this defect shipped 2026-08-21 before this guard
existed: `hooks.cater_subagent_start` (motivated the hooks-side sibling),
`op_census.report` (this workstream's own op, caught only by probing the
registry directly), and `sizing.read_object_fields` (found by
`test_registry_fast_path_matches_live_registry` and fixed by hand in
`3ba02f307` -- its own suite asserted registry membership and stayed GREEN
the entire time it was dead, because that suite imports the module directly
and the decorator fires as an import side effect on its OWN import, never
through the package's real eager-import path -- and, per the correction
above, this guard's OWN first revision would ALSO have missed it, since
`sizing.read_object_fields` was in `OP_MODULE_MAP` and the union rule counted
that as reachable).

WHY THIS GUARD DOES NOT IMPORT THE MODULES UNDER TEST (negative spec):
Any guard that imports (directly, or transitively via another
already-imported test module) the modules it is checking can never observe
the "declared but unreachable" state; it can only observe "was imported by
something, at some point, in this process" -- exactly the failure mode that
kept `sizing.read_object_fields`'s own suite green over a dead op (see
`coordinator_core/ops/__init__.py::_EAGER_OP_MODULES`'s own inline note on
that entry, added 2026-08-21, for the citable evidence of that specific
claim -- not a paraphrase of it). This module therefore parses
`coordinator_core/**/*.py` as TEXT with `ast` -- no `importlib`, no
`__import__`, no `subprocess`, and no importing `coordinator_core.ops`,
`coordinator_core.ops._registry_map`, or any module under test. It reads
`_EAGER_OP_MODULES` and `OP_MODULE_MAP` as plain Python list/dict literals
straight out of their own source files.

Scope and exclusions:
  - `coordinator_core/hooks/` is excluded entirely: it has its own sibling
    guard at package-file granularity (one entry per hooks/*.py module), a
    different shape than the ops package's mix of flat modules, per-op
    submodules under nested packages (e.g. `ops.tracker.*`, `ops.session.*`,
    `ops.session.*`), and the handful of non-`ops.*` top-level packages that
    also participate in `_EAGER_OP_MODULES` (`frontmatter`, `orientation`,
    `plugin_health`, `probes`, `goals`, `install`, `session_ledger`). Folding
    hooks into this guard would require reconciling two incompatible
    granularities for no completeness gain the sibling doesn't already cover.
  - Any path with a `tests` directory component, any `test_*.py` /
    `conftest.py` filename, and `__init__.py` files are excluded as
    non-op-handler plumbing -- the same class of exclusion the hooks sibling
    applies via `_NON_OP_MODULE_NAMES`, generalised to survive a nested tree
    where test modules are not confined to one flat directory.
  - This guard module itself lives under `coordinator_core/ops/tests/`, so
    the `tests`-directory exclusion above already keeps it out of its own
    scan without a separate self-exclusion list.

Spec backlink: this dispatch's task (completeness guard for the ops eager
list / registry map), sibling of the hooks-package guard the same incident
motivated.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CC_DIR = Path(__file__).resolve().parent.parent.parent
_OPS_INIT_PATH = _CC_DIR / "ops" / "__init__.py"
_REGISTRY_MAP_PATH = _CC_DIR / "ops" / "_registry_map.py"

_EXCLUDED_FILENAMES = {"__init__.py", "conftest.py"}


def _is_excluded(path: Path) -> bool:
    parts = path.relative_to(_CC_DIR).parts
    if "hooks" in parts:
        return True
    if "tests" in parts:
        return True
    if "__pycache__" in parts:
        return True
    if path.name in _EXCLUDED_FILENAMES or path.name.startswith("test_"):
        return True
    return False


def _module_dotted_path(path: Path) -> str:
    rel = path.relative_to(_CC_DIR.parent).with_suffix("")
    return ".".join(rel.parts)


def _find_module_level_list_or_dict(tree: ast.Module, name: str) -> ast.expr:
    """Locate `NAME = [...]` / `NAME: Ann = [...]` (or dict-valued) at module
    level, handling both `ast.Assign` and `ast.AnnAssign` -- the ops package's
    `_EAGER_OP_MODULES` and `OP_MODULE_MAP` are BOTH declared with an
    explicit type annotation (`List[Tuple[str, str]]` / `Dict[str, str]`),
    unlike the hooks sibling's plain `_EAGER_HOOK_MODULES: list[str] = [...]`
    -- so both branches must be checked here, not just one."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return value
    raise AssertionError(f"{name} not found as a module-level assignment")


def _load_eager_op_modules() -> list[str]:
    """Extract the dotted-module-path element of each `(module_path, note)`
    tuple in `_EAGER_OP_MODULES`, straight from `__init__.py`'s own source
    via `ast` -- never by importing `coordinator_core.ops` (which would run
    `_eager_import_all()` and register every op as a side effect, exactly the
    trap this guard exists to avoid)."""
    tree = ast.parse(_OPS_INIT_PATH.read_text(encoding="utf-8"), filename=str(_OPS_INIT_PATH))
    value = _find_module_level_list_or_dict(tree, "_EAGER_OP_MODULES")
    if not isinstance(value, ast.List):
        raise AssertionError(f"_EAGER_OP_MODULES in {_OPS_INIT_PATH} is not a list literal")
    entries: list[str] = []
    for elt in value.elts:
        if not isinstance(elt, ast.Tuple) or not elt.elts:
            continue
        first = elt.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            entries.append(first.value)
    return entries


def _load_op_module_map_values() -> set[str]:
    """Extract the dotted-module-path VALUES of `OP_MODULE_MAP`, straight
    from `_registry_map.py`'s own source via `ast` -- never by importing
    `coordinator_core.ops._registry_map` (whose own docstring already
    forbids importing any op module from it, so this guard holds that module
    to the same standard it imposes on itself)."""
    tree = ast.parse(_REGISTRY_MAP_PATH.read_text(encoding="utf-8"), filename=str(_REGISTRY_MAP_PATH))
    value = _find_module_level_list_or_dict(tree, "OP_MODULE_MAP")
    if not isinstance(value, ast.Dict):
        raise AssertionError(f"OP_MODULE_MAP in {_REGISTRY_MAP_PATH} is not a dict literal")
    values: set[str] = set()
    for v in value.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            values.add(v.value)
    return values


def _register_op_calls(node: ast.AST) -> list[ast.Call]:
    calls = []
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return calls
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        callee = dec.func
        if isinstance(callee, ast.Name) and callee.id == "register_op":
            calls.append(dec)
        elif isinstance(callee, ast.Attribute) and callee.attr == "register_op":
            calls.append(dec)
    return calls


def _modules_declaring_register_op() -> dict[str, int]:
    """Walk every non-excluded `coordinator_core/**/*.py` module on the
    filesystem and return {dotted module path: count of @register_op-decorated
    functions found}, for any module with at least one.

    Walks the FULL ast tree (not just module-level statements) so a
    register_op-decorated function nested inside a class or another function
    is still caught -- the ops package's tree is deeper and less uniform than
    the flat hooks directory the sibling guard covers, so this guard cannot
    assume top-level-only placement the way that one safely does."""
    declared: dict[str, int] = {}
    for path in sorted(_CC_DIR.rglob("*.py")):
        if _is_excluded(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count = 0
        for node in ast.walk(tree):
            count += len(_register_op_calls(node))
        if count:
            declared[_module_dotted_path(path)] = count
    return declared


def _missing_from_reachable_set(declared: dict[str, int], reachable: set[str]) -> dict[str, int]:
    return {mod: n for mod, n in declared.items() if mod not in reachable}


def test_every_register_op_module_is_reachable_via_eager_list() -> None:
    declared = _modules_declaring_register_op()
    eager_list = _load_eager_op_modules()
    reachable = set(eager_list)

    missing = _missing_from_reachable_set(declared, reachable)

    assert not missing, (
        "The following coordinator_core module(s) declare @register_op but are "
        "NOT reachable through _EAGER_OP_MODULES (coordinator_core/ops/"
        "__init__.py): "
        + ", ".join(sorted(missing))
        + ". A decorator alone does not register an op, and OP_MODULE_MAP "
        "presence does not confer reachability either (see module docstring's "
        "CORRECTED RULE section) -- only _EAGER_OP_MODULES membership makes "
        "`import coordinator_core.ops` actually register the op. Absent that, "
        "the op(s) ship present-but-dead and coordinator-invoke cannot resolve "
        "them (registry MISS at dispatch time). Add the dotted module path(s) "
        "to _EAGER_OP_MODULES."
    )


# ---------------------------------------------------------------------------
# Proof the guard bites: exercise the comparison function directly with a
# synthetic missing entry, rather than mutating the real eager list / map.
# ---------------------------------------------------------------------------

def test_missing_from_reachable_set_detects_a_synthetic_gap() -> None:
    declared = {
        "coordinator_core.ops.real_module": 1,
        "coordinator_core.ops.orphaned_module": 1,
    }
    reachable = {"coordinator_core.ops.real_module"}  # orphaned_module deliberately omitted

    missing = _missing_from_reachable_set(declared, reachable)

    assert missing == {"coordinator_core.ops.orphaned_module": 1}


def test_missing_from_reachable_set_rejects_op_module_map_only_reachability() -> None:
    """Reproduces the exact `sizing.read_object_fields` incident state: a module
    decorated with @register_op, present in OP_MODULE_MAP, but ABSENT from
    _EAGER_OP_MODULES. Under the old (wrong) union-of-both-seams rule, this
    module's presence in OP_MODULE_MAP alone would have made it count as
    reachable and the guard would have passed -- exactly the failure mode that
    let `sizing.read_object_fields` ship dead through three incidents (see
    module docstring's CORRECTED RULE). The reachable set passed here
    deliberately does NOT include OP_MODULE_MAP values, matching this guard's
    corrected `reachable = set(eager_list)` computation, so this test would
    have failed under the old `reachable = set(eager_list) | map_values` code."""
    declared = {
        "coordinator_core.ops.sizing": 1,
    }
    # OP_MODULE_MAP-only reachability -- deliberately not folded into `reachable`.
    reachable: set[str] = set()  # sizing.read_object_fields's module is NOT in _EAGER_OP_MODULES

    missing = _missing_from_reachable_set(declared, reachable)

    assert missing == {"coordinator_core.ops.sizing": 1}


def test_ops_and_registry_map_sources_parse_and_yield_nonempty_lists() -> None:
    """Sanity check that this guard's own extraction logic is keeping up with
    the real source files it reads -- an empty result from either loader
    would make the completeness check above vacuously pass."""
    eager_list = _load_eager_op_modules()
    map_values = _load_op_module_map_values()
    assert len(eager_list) > 50
    assert len(map_values) > 50
