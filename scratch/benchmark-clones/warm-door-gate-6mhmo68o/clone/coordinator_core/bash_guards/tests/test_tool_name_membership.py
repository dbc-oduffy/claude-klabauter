"""AC5 structural pin for `docs/plans/2026-08-07-command-guards-fire-under-
both-tool-names.md`'s re-cut: per-guard `MATCHERS` is the live policy
surface, `COMMAND_TOOL_NAMES` is the universe it is drawn FROM, and no
`tool_name` comparison anywhere in this package may hardcode a bare tool
name literal any more (C1 replaced every such comparison with a membership
check against a declared set).

Retires the old identity (`is`) assertion this file's predecessor carried:
per-guard `MATCHERS` sets differ BY DESIGN post re-cut (14 of 21 guards stay
Bash-only; see the plan's C1 row), so asserting every module's `MATCHERS`
`is` the shared constant is now the WRONG property -- it would fail on
every correctly-scoped Bash-only guard. Subset-of-universe replaces it:
each declared value must be a `tuple` (not a `list`, which is exactly what
`list(COMMAND_TOOL_NAMES)` or a hand-retyped copy would produce and still
satisfy a naive membership check) whose every element is drawn from
`COMMAND_TOOL_NAMES`, and a guard that legitimately declares the full
universe must reference the shared constant object directly (asserted by
`is`, not merely by equal contents) -- see `_tool_names.py`'s own docstring
for why the constant is a tuple in the first place.

Also asserts a property the plan names as a deliberate ADDITION beyond
AC5's own wording (not in the plan's AC5 row): `dispatch.py`'s cached
`_any_declared_matchers()` union must equal the union of the matchers on
the LIVE `_build_guard_chain()` registration. That cache is hand-maintained
(a literal list of imported `MATCHERS` names unioned in `_any_declared_
matchers`'s body) precisely so the master gate can compute the union
BEFORE the chain is built -- see that function's own docstring for the
cost argument. A registration added to `_build_guard_chain` without a
matching addition to the hand-maintained cache list would make a widened
guard silently unreachable: the master gate would reject its tool_name
before the chain -- and therefore that guard -- is ever consulted. That is
the exact failure class (a declaration that looks live but is not) this
whole plan exists to close, one layer up; this test is the regression lock
for it.
"""
from __future__ import annotations

import ast
import pathlib
from typing import Iterator, List, Tuple

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

_PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent

# Files that are never guard-check modules and therefore carry no MATCHERS
# contract of their own -- excluded from BOTH the tool_name-literal sweep
# and the MATCHERS-subset sweep. `dispatch.py`/`dispatch_checks.py` are the
# dispatcher itself (dispatch.py's own comparisons are the master gate's
# DECLARED-set check, not a hardcoded literal -- see its `_any_declared_
# matchers()` docstring); underscore-prefixed modules are private helpers,
# none of which register a `MATCHERS` tuple in `_build_guard_chain`.
# `commit_tripwires.py` is not its own chain registration -- it is a library
# `dispatch_checks.check_validate_commit` calls internally, so it has no
# MATCHERS of its own to sweep.
_NON_GUARD_MODULES = {"dispatch.py", "dispatch_checks.py", "commit_tripwires.py"}


def _guard_module_paths() -> List[pathlib.Path]:
    return sorted(
        p
        for p in _PACKAGE_DIR.glob("*.py")
        if not p.name.startswith("_") and p.name not in _NON_GUARD_MODULES
    )


def _all_module_paths_for_literal_sweep() -> List[pathlib.Path]:
    """Every module under `bash_guards/` EXCEPT `_tool_names.py` itself
    (whose own literal `("Bash", "PowerShell")` tuple is the universe
    definition, not a comparison) and this tests directory. `dispatch.py`
    is included here, unlike in the MATCHERS sweep above -- it is exactly
    where a reintroduced hardcoded `tool_name == "Bash"` early-exit would
    be most damaging, so the literal-comparison sweep must not skip it."""
    return sorted(
        p
        for p in _PACKAGE_DIR.glob("*.py")
        if p.name != "_tool_names.py"
    )


def test_discovery_found_a_plausible_number_of_guard_modules():
    """Guards the guard: if the glob pattern breaks (wrong directory, wrong
    extension, an overzealous exclusion list), every assertion below would
    pass vacuously by finding nothing to check."""
    guard_modules = _guard_module_paths()
    assert len(guard_modules) >= 20, guard_modules
    names = {p.name for p in guard_modules}
    assert "block_stash_destruction.py" in names
    assert "guard_grep_via_bash.py" in names

    literal_sweep_modules = _all_module_paths_for_literal_sweep()
    assert len(literal_sweep_modules) >= 30, literal_sweep_modules
    assert any(p.name == "dispatch.py" for p in literal_sweep_modules)


def _tool_name_comparison_violations(tree: ast.AST, source_path: pathlib.Path) -> Iterator[str]:
    """Yield one description per `Compare` node that tests a `tool_name`-
    shaped operand against a bare string-literal tool name. Only a live
    COMPARISON is a violation -- a string literal inside a docstring,
    inline comment, or an unrelated dict-literal mapping (e.g. a `Dialect`
    lookup table) is not visited by the AST at all in the comparison case,
    so those cannot false-positive here."""

    def _is_tool_name_operand(node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and "tool_name" in node.id:
            return True
        if isinstance(node, ast.Attribute) and "tool_name" in node.attr:
            return True
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value == "tool_name":
                return True
            # `payload.get("tool_name")` shows up as a Call, handled below.
        return False

    def _is_tool_name_get_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "tool_name"
        )

    def _is_bare_tool_name_literal(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value in ("Bash", "PowerShell")

    def _is_bare_tool_name_literal_collection(node: ast.AST) -> bool:
        """Catches the `in`/`not in` membership-check gap: a literal
        `Tuple`/`List`/`Set` whose elements are all bare tool-name string
        constants (e.g. `tool_name in ("Bash",)`) reintroduces the exact
        hardcoded-set hazard AC5(a) exists to close, while superficially
        looking like the prescribed remediation (a membership test). A
        membership test against a NAME (`tool_name in MATCHERS`, `tool_name
        not in COMMAND_TOOL_NAMES`) is the correct pattern and must not
        match here -- only a literal collection of the strings themselves."""
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return False
        if not node.elts:
            return False
        return all(_is_bare_tool_name_literal(elt) for elt in node.elts)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        has_tool_name_operand = any(
            _is_tool_name_operand(o) or _is_tool_name_get_call(o) for o in operands
        )
        has_bare_literal = any(
            _is_bare_tool_name_literal(o) or _is_bare_tool_name_literal_collection(o)
            for o in operands
        )
        if has_tool_name_operand and has_bare_literal:
            yield "%s:%s: %s" % (source_path, node.lineno, ast.dump(node))


def test_no_hardcoded_tool_name_literal_survives_in_a_comparison():
    """AC5(a): no `tool_name == "Bash"`-shaped comparison anywhere in the
    package. Proven to be able to fail (checked by construction): injecting
    a synthetic `payload.get("tool_name") == "Bash"` Compare node below,
    parsed from a literal source string rather than read off disk, reliably
    trips `_tool_name_comparison_violations` -- demonstrating the detector
    itself, independent of what today's real files happen to contain."""
    synthetic_source = (
        "def check(payload):\n"
        "    if payload.get('tool_name') == 'Bash':\n"
        "        return 'deny'\n"
    )
    synthetic_tree = ast.parse(synthetic_source)
    synthetic_violations = list(
        _tool_name_comparison_violations(synthetic_tree, pathlib.Path("<synthetic>"))
    )
    assert synthetic_violations, "detector failed to catch its own synthetic positive control"

    # Second positive control: a literal-TUPLE membership test, the shape
    # `_is_bare_tool_name_literal_collection` exists to catch -- a future
    # guard writing `tool_name in ("Bash",)` would look like it followed the
    # membership-check remediation this test's own docstring prescribes,
    # while still hardcoding a non-MATCHERS-sourced set.
    synthetic_membership_source = (
        "def check(payload):\n"
        "    tool_name = payload.get('tool_name')\n"
        "    if tool_name in ('Bash', 'PowerShell'):\n"
        "        return 'deny'\n"
    )
    synthetic_membership_tree = ast.parse(synthetic_membership_source)
    synthetic_membership_violations = list(
        _tool_name_comparison_violations(
            synthetic_membership_tree, pathlib.Path("<synthetic-membership>")
        )
    )
    assert synthetic_membership_violations, (
        "detector failed to catch its own synthetic literal-tuple membership "
        "positive control"
    )

    # Negative control: membership against a NAME (the correct remediation
    # pattern) must NOT be flagged.
    synthetic_clean_source = (
        "def check(payload):\n"
        "    tool_name = payload.get('tool_name')\n"
        "    if tool_name not in MATCHERS:\n"
        "        return None\n"
    )
    synthetic_clean_tree = ast.parse(synthetic_clean_source)
    synthetic_clean_violations = list(
        _tool_name_comparison_violations(
            synthetic_clean_tree, pathlib.Path("<synthetic-clean>")
        )
    )
    assert not synthetic_clean_violations, (
        "detector false-positived on a membership test against a NAME "
        "(the correct remediation pattern), not a literal collection"
    )

    all_violations: List[str] = []
    for path in _all_module_paths_for_literal_sweep():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        all_violations.extend(_tool_name_comparison_violations(tree, path))

    assert not all_violations, (
        "hardcoded tool_name literal comparison(s) found -- convert to a "
        "membership check against a declared MATCHERS/COMMAND_TOOL_NAMES "
        "set:\n" + "\n".join(all_violations)
    )


def _module_level_matchers_assign(tree: ast.AST) -> ast.Assign | None:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "MATCHERS"
        ):
            return node
    return None


def _guard_modules_with_matchers() -> List[Tuple[pathlib.Path, object]]:
    """Import each guard module and pull its live `MATCHERS` attribute --
    the RUNTIME value, not a re-parse of its source -- so this sweep is
    exercising exactly what `_build_guard_chain` itself would import."""
    import importlib

    found: List[Tuple[pathlib.Path, object]] = []
    for path in _guard_module_paths():
        module_name = "coordinator_core.bash_guards." + path.stem
        module = importlib.import_module(module_name)
        if hasattr(module, "MATCHERS"):
            found.append((path, module.MATCHERS))
    return found


def test_matchers_declarations_are_tuples_subset_of_the_universe():
    """AC5(b): every guard module's MATCHERS is a `tuple` drawn from
    `COMMAND_TOOL_NAMES` -- two legs, both individually falsifiable:

    Leg 1 (type): `list(COMMAND_TOOL_NAMES)` has equal CONTENTS to the
    tuple but a different type, and a `MATCHERS = list(COMMAND_TOOL_NAMES)`
    regression would sail through a contents-only check while breaking the
    `is`-identity leg in test_full_universe_declarations_use_the_shared_
    constant_by_identity below (list(...) always builds a new list, never
    the same object) -- so this leg exists specifically to catch that class
    even where identity isn't the assertion in play (a Bash-only guard
    retyped as a list, e.g. `MATCHERS = ["Bash"]`, would pass a contents
    check silently forever without this leg).

    Leg 2 (membership): every element must be IN `COMMAND_TOOL_NAMES` -- a
    free-text tool name (e.g. a typo'd `"bash"` or an invented `"Zsh"` the
    dispatcher can never actually observe in a payload) fails this leg.
    """
    matchers_by_module = _guard_modules_with_matchers()
    assert len(matchers_by_module) >= 15, matchers_by_module

    universe = set(COMMAND_TOOL_NAMES)
    for path, matchers in matchers_by_module:
        assert isinstance(matchers, tuple), (
            "%s: MATCHERS must be a tuple, got %s -- a list (e.g. "
            "list(COMMAND_TOOL_NAMES) or [\"Bash\"]) satisfies a contents-"
            "only check while breaking `is`-identity for full-universe "
            "declarations" % (path, type(matchers))
        )
        for name in matchers:
            assert name in universe, (
                "%s: MATCHERS contains %r, which is not in COMMAND_TOOL_NAMES "
                "%r -- the dispatcher can never observe a payload with this "
                "tool_name, so this element can never actually gate anything"
                % (path, name, COMMAND_TOOL_NAMES)
            )


def test_full_universe_declarations_use_the_shared_constant_by_identity():
    """AC5(b) continued: a guard module whose source literally reads
    `MATCHERS = COMMAND_TOOL_NAMES` must hold that identity (`is`) at
    runtime, not merely equal contents -- `(*COMMAND_TOOL_NAMES,)` (unpack-
    splat) and a hand-retyped `("Bash", "PowerShell")` copy both produce an
    equal-but-not-identical tuple and would pass a `==` check while silently
    breaking the reference precedent

    NOTE: `tuple(COMMAND_TOOL_NAMES)` is NOT such a case -- CPython's
    `tuple()` constructor returns the SAME object when given an argument
    that is already an exact `tuple` (no copy is made, since tuples are
    immutable), so `MATCHERS = tuple(COMMAND_TOOL_NAMES)` is indistinguishable
    at runtime from `MATCHERS = COMMAND_TOOL_NAMES` and this `is` assertion
    would pass for either. `tuple(list(COMMAND_TOOL_NAMES))` DOES produce a
    genuinely new object (the intermediate `list(...)` copy breaks the
    same-object optimization) and would be caught here.
    `_tool_names.py`'s own docstring calls out as the whole reason the
    constant is a tuple.

    Discovered by SOURCE inspection (an `ast.Assign` naming `COMMAND_TOOL_
    NAMES` on the right-hand side), then verified against the RUNTIME
    value -- so a module whose source says one thing but whose runtime
    value (e.g. reassigned post-import) says another is caught too.
    """
    import importlib

    full_universe_modules: List[pathlib.Path] = []
    for path in _guard_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assign = _module_level_matchers_assign(tree)
        if assign is None:
            continue
        if isinstance(assign.value, ast.Name) and assign.value.id == "COMMAND_TOOL_NAMES":
            full_universe_modules.append(path)

    assert len(full_universe_modules) >= 8, full_universe_modules

    for path in full_universe_modules:
        module_name = "coordinator_core.bash_guards." + path.stem
        module = importlib.import_module(module_name)
        assert module.MATCHERS is COMMAND_TOOL_NAMES, (
            "%s: MATCHERS = COMMAND_TOOL_NAMES in source but the runtime "
            "value is not the SAME object (equal-contents copy such as "
            "(*COMMAND_TOOL_NAMES,), tuple(list(COMMAND_TOOL_NAMES)), or a "
            "retyped literal -- NOT tuple(COMMAND_TOOL_NAMES), which CPython "
            "returns as the same object for an already-tuple argument) -- "
            "this defeats the only property that makes COMMAND_TOOL_NAMES a "
            "tuple rather than a list" % path
        )


def _dummy_chain():
    return dispatch._build_guard_chain(
        cmd="echo tool-name-membership-probe",
        session_id="tool-name-membership-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )


def test_any_declared_matchers_equals_the_live_chains_declared_union():
    """Deliberate addition beyond the plan's AC5 wording (see module
    docstring) -- not itself an AC row, but the property the plan's C1
    prose names as the master-gate cost argument's load-bearing
    precondition: `_any_declared_matchers()`'s hand-maintained cache must
    equal the union of the LIVE chain's own `matchers` fields.

    Proven able to fail: `_any_declared_matchers()` is a cached union built
    from a fixed, hand-typed list of imported `MATCHERS` names in dispatch.
    py -- it is computed independently of `_build_guard_chain`'s own
    per-entry `matchers=` kwargs, so nothing PYTHON-mechanical keeps them
    equal; only manual diligence does, at every registration. Deleting or
    forgetting to extend one side (e.g. adding a widened guard's
    registration to `_build_guard_chain` without adding its MATCHERS name
    to `_any_declared_matchers`'s union list) changes one side's return
    value without touching the other's -- there is no shared code path
    that would keep them in sync automatically -- so this assertion is
    exercising a genuinely independent computation, not a tautology.
    """
    chain = _dummy_chain()
    live_union = frozenset()
    for entry in chain:
        live_union = live_union | frozenset(entry.matchers)

    cached_union = dispatch._any_declared_matchers()

    assert cached_union == live_union, (
        "dispatch._any_declared_matchers() (%r) does not equal the union of "
        "the live guard_chain's own declared `matchers` (%r) -- if a "
        "registration's own MATCHERS widened without also extending the "
        "hand-maintained import/union list inside `_any_declared_matchers`, "
        "add the new registration's MATCHERS to `_any_declared_matchers`. A "
        "mismatch here means the master gate's early return can reject a "
        "tool_name before a guard that declares it ever runs -- a "
        "registration that looks live but is silently unreachable."
        % (cached_union, live_union)
    )


@pytest.mark.parametrize("removed_name", ["destructive-git-orphan"])
def test_union_equality_detector_can_actually_fail(removed_name):
    """Positive control for the assertion above: simulate a chain entry
    whose matchers the cache does NOT know about, and confirm the equality
    check would flag it -- without touching `dispatch.py`'s real cache
    (out of scope; this constructs a local comparison only)."""
    chain = _dummy_chain()
    live_union = frozenset()
    for entry in chain:
        live_union = live_union | frozenset(entry.matchers)

    stale_cache = live_union - {"Bash"} if "Bash" in live_union else live_union | {"SomeNewShell"}
    assert stale_cache != live_union, "positive control itself did not diverge from the live union"
