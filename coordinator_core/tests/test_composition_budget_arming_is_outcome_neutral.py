"""AC5/AC6 pin (chunk C7, docs/plans/2026-08-18-arm-the-composition-budget.md):
Finding 3 argues that arming the two fleet ceilings
(`coordinator_core.composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET` /
`FLEET_MAX_INVOCATIONS`, both `None` today -- a pure recorder) is
OUTCOME-NEUTRAL by construction. This module turns that argument into a
test, because "verified, not assumed" is the stub's own wording.

THREE ASSERTIONS, deliberately split by what each one actually pins:

(0) CONSTRUCTION SITE -- the discharge of AC5's own wording ("fails if a
    future edit moves budget construction away from the composition's own
    entry"). Structural (AST), not a value check on the factory's output --
    a value check stays green no matter where production code calls the
    factory, so hoisting the call to module scope would leave it passing.
    NOT `designed_red` -- true as soon as C2/C3 land, which they have.

(1) UNREACHABILITY (`designed_red`) -- the two fleet ceilings are armed
    (non-`None`, positive) and a budget checked immediately after
    construction is within budget on both.

(2) EQUIVALENCE (`designed_red`) -- for one caller from each lineage
    (`contract.apply_base.execute_directives`, and one of the three
    `apply_halt` ceremonies' own `_execute_directives`), driving the
    directive path twice -- once with a budget, once with `None` -- yields
    an identical exit code and report shape, except the additive
    `budget_breach` key being absent in both.

(1) and (2) are marked `designed_red`: with the fleet constants at `None`
from C1 through C4's multi-day soak, a red-by-default test in a SHARED TREE
running the fast tier is a fleet-visible break for a dozen concurrent
sessions, not an informative failure. C5's deliverable includes un-marking
them.

Spec backlink: docs/plans/2026-08-18-arm-the-composition-budget.md § C7
               state/subagent-share/3d70362f-6845-47ec-901e-3b9f0b412836/PINNED-INTERFACE.md
               coordinator_core/tests/test_composition_budget_is_armed.py (AST technique)
               coordinator_core/tests/test_composition_budget_boundaries.py (drive pattern)
"""
from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from coordinator_core import composition_budget as _composition_budget
from coordinator_core.composition_budget import CompositionBudget
from coordinator_core.contract import apply_base
from coordinator_core.telemetry.composition_record import make_fleet_budget
from coordinator_core.workday_complete import apply as workday_complete_apply

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# (0) Construction site -- AST assertion, same technique as
# test_composition_budget_is_armed.py: parse the file, find the top-level
# `def apply(...)`, and confirm `make_fleet_budget` is called from a
# statement inside that function's own body -- never at module scope, never
# inside a cached-global initializer (which, syntactically, is also never
# inside `def apply`'s own body).
# ---------------------------------------------------------------------------

#: All 8 apply.py callers named in the pinned interface: the 5 apply_base
#: callers (C2) plus the 3 apply_halt ceremonies (C3).
_ALL_EIGHT_APPLY_FILES: tuple[str, ...] = (
    "coordinator_core/backlog_grind_assemble/apply.py",
    "coordinator_core/baton_assemble/apply.py",
    "coordinator_core/consolidate_assemble/apply.py",
    "coordinator_core/merge_assemble/apply.py",
    "coordinator_core/pickup_assemble/apply.py",
    "coordinator_core/workday_complete/apply.py",
    "coordinator_core/workstream_complete/apply.py",
    "coordinator_core/workweek_complete/apply.py",
)


def _call_name(node: ast.expr) -> "str | None":
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _find_apply_def(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "apply":
            return node
    raise AssertionError("no top-level `def apply(...)` found")


def _make_fleet_budget_call_is_inside_apply_entry(source: str) -> bool:
    """`True` iff a call to `make_fleet_budget` appears anywhere inside the
    top-level `def apply(...)`'s own AST subtree. Since `ast.walk` here is
    scoped to `apply_def` alone (never the module tree), a call hoisted to
    module scope -- or moved into a module-level cached-singleton
    initializer, which is by definition NOT inside `def apply`'s body --
    is invisible to this walk and the check correctly fails."""
    tree = ast.parse(source)
    apply_def = _find_apply_def(tree)
    for node in ast.walk(apply_def):
        if isinstance(node, ast.Call) and _call_name(node.func) == "make_fleet_budget":
            return True
    return False


@pytest.mark.parametrize("relpath", _ALL_EIGHT_APPLY_FILES, ids=_ALL_EIGHT_APPLY_FILES)
def test_make_fleet_budget_is_constructed_inside_apply_entry(relpath: str) -> None:
    path = _REPO_ROOT / relpath
    source = path.read_text(encoding="utf-8")
    assert _make_fleet_budget_call_is_inside_apply_entry(source), (
        f"{relpath}: make_fleet_budget(...) must be called from inside the "
        "top-level def apply(...) -- never at module scope, never from a "
        "cached-global initializer (AC5: budget construction stays at the "
        "composition's own entry)"
    )


# ---------------------------------------------------------------------------
# Detector self-tests -- planted fixtures proving the check fires on the
# regression it exists to catch, not merely on the 8 real files passing
# today (mirrors test_composition_budget_is_armed.py's own self-tests).
# ---------------------------------------------------------------------------

_ARMED_FIXTURE = """
def apply():
    composition_budget = make_fleet_budget("x")
    try:
        return _execute_directives(a, b, c, composition_budget=composition_budget)
    finally:
        flush_composition_record(composition_budget, "success")
"""

_MODULE_SCOPE_FIXTURE = """
_BUDGET = make_fleet_budget("x")


def apply():
    try:
        return _execute_directives(a, b, c, composition_budget=_BUDGET)
    finally:
        flush_composition_record(_BUDGET, "success")
"""

_CACHED_GLOBAL_INITIALIZER_FIXTURE = """
_BUDGET = None


def _get_budget():
    global _BUDGET
    if _BUDGET is None:
        _BUDGET = make_fleet_budget("x")
    return _BUDGET


def apply():
    composition_budget = _get_budget()
    try:
        return _execute_directives(a, b, c, composition_budget=composition_budget)
    finally:
        flush_composition_record(composition_budget, "success")
"""


def test_detector_accepts_construction_inside_apply_entry() -> None:
    assert _make_fleet_budget_call_is_inside_apply_entry(_ARMED_FIXTURE)


@pytest.mark.parametrize(
    "fixture",
    [_MODULE_SCOPE_FIXTURE, _CACHED_GLOBAL_INITIALIZER_FIXTURE],
    ids=["module-scope-singleton", "cached-global-initializer"],
)
def test_detector_rejects_construction_outside_apply_entry(fixture: str) -> None:
    assert not _make_fleet_budget_call_is_inside_apply_entry(fixture)


# ---------------------------------------------------------------------------
# (1) Unreachability -- designed_red until C5 arms the two fleet ceilings.
# ---------------------------------------------------------------------------


@pytest.mark.designed_red
def test_armed_fleet_ceilings_leave_a_fresh_budget_within_budget() -> None:
    """Once C5 arms the ceilings, both must be positive (an armed count
    ceiling of >= 1, an armed elapsed ceiling of > 0 -- see C5), and a
    budget checked immediately after construction (elapsed ~0, invocation
    count 0) must sit within both -- the one outcome-changing boundary
    (`check()` at the pre-mutation seam) cannot fire on a healthy run.

    Fails today: both constants are `None` (C1 shipped a pure recorder),
    so the `>= 1` / `> 0` comparisons below fail outright."""
    assert _composition_budget.FLEET_MAX_INVOCATIONS is not None
    assert _composition_budget.FLEET_MAX_INVOCATIONS >= 1
    assert _composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET is not None
    assert _composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET > 0

    budget = make_fleet_budget("test-unreachability-pin")
    assert budget.invocation_count == 0
    assert budget.elapsed_secs() >= 0
    assert budget.check() is False


# ---------------------------------------------------------------------------
# (2) Equivalence -- designed_red until C5 arms the two fleet ceilings.
# One caller from each lineage: apply_base.execute_directives (C2's
# lineage) and workday_complete.apply._execute_directives (C3's apply_halt
# lineage, § test_composition_budget_boundaries.py's own drive pattern).
# Both callers accept an `already_satisfied` directive that lands without
# any real CLI dispatch, keeping this structural rather than
# timing-dependent.
# ---------------------------------------------------------------------------


def _assert_equivalent_reports(
    report_with_budget: dict[str, Any], report_without_budget: dict[str, Any]
) -> None:
    """Identical except the additive `budget_breach` key being absent in
    BOTH -- never present on either side. A present key on the budgeted
    side alone would mean the armed ceilings actually breached against
    this trivial fixture, which is itself the AC5/AC6 failure this test
    exists to catch (see module docstring: STOP and report, don't tolerate
    it)."""
    assert "budget_breach" not in report_with_budget
    assert "budget_breach" not in report_without_budget
    assert report_with_budget == report_without_budget


@pytest.mark.designed_red
def test_apply_base_lineage_arming_is_outcome_neutral(tmp_path) -> None:
    """Fails today for the same reason as (1): the fleet ceilings are
    `None`, so `make_fleet_budget` builds a pure recorder whose ceiling
    values below are `None` rather than the armed positive values C5
    ships -- this test pins the post-C5 promise, not today's unarmed
    state."""
    assert _composition_budget.FLEET_MAX_INVOCATIONS is not None
    assert _composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET is not None

    def _ok_handler(args: list[str], repo_root: pathlib.Path) -> dict[str, Any]:
        return {"ok": True}

    directives = [{"id": "d1", "cli": "noop", "args": []}]

    budget = make_fleet_budget("test-equivalence-apply-base")
    rc_with_budget, report_with_budget = apply_base.execute_directives(
        directives=directives,
        judgment_points=[],
        repo_root=tmp_path,
        dispatch_table={"noop": _ok_handler},
        composition_budget=budget,
    )
    rc_without_budget, report_without_budget = apply_base.execute_directives(
        directives=directives,
        judgment_points=[],
        repo_root=tmp_path,
        dispatch_table={"noop": _ok_handler},
        composition_budget=None,
    )
    assert rc_with_budget == rc_without_budget
    _assert_equivalent_reports(report_with_budget, report_without_budget)


@pytest.mark.designed_red
def test_apply_halt_lineage_arming_is_outcome_neutral() -> None:
    """Same pin as the apply_base lineage above, driven through one of the
    three `apply_halt` ceremonies' own `_execute_directives`
    (`apply_halt` itself owns no loop -- see
    test_composition_budget_boundaries.py's own
    `TestApplyHaltBoundaries.test_breach_never_reaches_a_compensation_pass`
    docstring). An `already_satisfied` directive lands without loading any
    real consumes-manifest CLI, keeping this structural."""
    assert _composition_budget.FLEET_MAX_INVOCATIONS is not None
    assert _composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET is not None

    directives = [{"id": "d1", "already_satisfied": True}]

    budget: CompositionBudget = make_fleet_budget("test-equivalence-apply-halt")
    rc_with_budget, report_with_budget = workday_complete_apply._execute_directives(
        directives, [], {}, composition_budget=budget
    )
    rc_without_budget, report_without_budget = workday_complete_apply._execute_directives(
        directives, [], {}, composition_budget=None
    )
    assert rc_with_budget == rc_without_budget
    _assert_equivalent_reports(report_with_budget, report_without_budget)
