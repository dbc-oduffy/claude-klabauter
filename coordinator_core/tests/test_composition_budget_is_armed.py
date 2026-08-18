"""AST regression pin (chunk C2, docs/plans/2026-08-18-arm-the-composition-
budget.md): the five `apply_base.execute_directives` callers this chunk arms
must each construct and thread a real `composition_budget`, and must flush
its record in a `finally` covering the same call -- never silently regress
to `execute_directives`'s own `composition_budget=None` default.

WHY AST, NOT GREP (same rationale as `test_no_unbatched_per_item_git_spawn.py`,
read first per the chunk brief): a grep for the substring `composition_budget=`
cannot tell a real threaded budget apart from a call site that writes
`composition_budget=None` explicitly, or from a `flush_composition_record(...)`
call that sits outside the `try/finally` that actually wraps the mutating
call. Both are the exact silent-regression shape this test exists to catch,
so the check inspects the `apply()` function's own `ast.Try` structure: the
call to `execute_directives`/`_execute_directives` must appear in the `try`
body with a `composition_budget` keyword whose value is not a literal
`None`, and a call to `flush_composition_record` must appear in that SAME
`Try` node's `finalbody`.

Source only -- this module never imports any of the five apply modules or
`coordinator_core.telemetry.composition_record`, so it passes whether or not
C1's `composition_record.py` has landed yet (its own factory functions are
not invoked here, only cited by name in the AST).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: file -> the top-level `def apply(...)` in it that must arm the budget.
#: `baton_assemble`/`pickup_assemble` dispatch through their own
#: `_execute_directives` wrapper (see each module's own wrapper docstring);
#: the other three call `apply_base.execute_directives` directly.
_SITES: tuple[tuple[str, str], ...] = (
    ("coordinator_core/backlog_grind_assemble/apply.py", "execute_directives"),
    ("coordinator_core/consolidate_assemble/apply.py", "execute_directives"),
    ("coordinator_core/merge_assemble/apply.py", "execute_directives"),
    ("coordinator_core/baton_assemble/apply.py", "_execute_directives"),
    ("coordinator_core/pickup_assemble/apply.py", "_execute_directives"),
)


def _call_name(node: ast.expr) -> "str | None":
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _calls_in(stmts: list[ast.stmt]):
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                yield node


def _find_apply_def(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "apply":
            return node
    raise AssertionError("no top-level `def apply(...)` found")


def _armed_composition_budget_kw(call: ast.Call) -> "ast.keyword | None":
    for kw in call.keywords:
        if kw.arg == "composition_budget":
            return kw
    return None


def _is_literal_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _try_arms_budget(try_node: ast.Try, execute_call_name: str) -> bool:
    """`True` iff `try_node.body` calls `execute_call_name` with a non-`None`
    `composition_budget=` keyword, AND `try_node.finalbody` calls
    `flush_composition_record` -- both required, in the SAME `Try`."""
    execute_call = None
    for call in _calls_in(try_node.body):
        if _call_name(call.func) == execute_call_name:
            execute_call = call
            break
    if execute_call is None:
        return False

    budget_kw = _armed_composition_budget_kw(execute_call)
    if budget_kw is None or _is_literal_none(budget_kw.value):
        return False

    flush_called = any(
        _call_name(call.func) == "flush_composition_record" for call in _calls_in(try_node.finalbody)
    )
    return flush_called


def _apply_arms_composition_budget(source: str, execute_call_name: str) -> bool:
    tree = ast.parse(source)
    apply_def = _find_apply_def(tree)
    for node in ast.walk(apply_def):
        if isinstance(node, ast.Try) and _try_arms_budget(node, execute_call_name):
            return True
    return False


@pytest.mark.parametrize("relpath,execute_call_name", _SITES, ids=[s[0] for s in _SITES])
def test_apply_entry_arms_composition_budget(relpath: str, execute_call_name: str) -> None:
    path = _REPO_ROOT / relpath
    source = path.read_text(encoding="utf-8")
    assert _apply_arms_composition_budget(source, execute_call_name), (
        f"{relpath}: apply() must call {execute_call_name}(...) with a non-None "
        "composition_budget= inside a try whose finally flushes the record "
        "(coordinator_core/telemetry/composition_record.py :: flush_composition_record)"
    )


# ---------------------------------------------------------------------------
# Detector self-tests -- planted fixtures proving the check actually fires on
# the regressions it exists to catch, not merely on the five real files
# passing today.
# ---------------------------------------------------------------------------

_ARMED_FIXTURE = """
def apply():
    budget = make_fleet_budget("x")
    outcome = "directive_failed"
    try:
        exit_code, report = execute_directives(a, b, c, composition_budget=budget)
        outcome = "success"
    finally:
        flush_composition_record(budget, outcome)
    return exit_code, report
"""

_NONE_BUDGET_FIXTURE = """
def apply():
    try:
        exit_code, report = execute_directives(a, b, c, composition_budget=None)
    finally:
        flush_composition_record(None, "directive_failed")
    return exit_code, report
"""

_MISSING_BUDGET_KWARG_FIXTURE = """
def apply():
    try:
        exit_code, report = execute_directives(a, b, c)
    finally:
        flush_composition_record(budget, "directive_failed")
    return exit_code, report
"""

_NO_FLUSH_FIXTURE = """
def apply():
    budget = make_fleet_budget("x")
    exit_code, report = execute_directives(a, b, c, composition_budget=budget)
    return exit_code, report
"""

_FLUSH_OUTSIDE_FINALLY_FIXTURE = """
def apply():
    budget = make_fleet_budget("x")
    try:
        exit_code, report = execute_directives(a, b, c, composition_budget=budget)
    except Exception:
        raise
    flush_composition_record(budget, "success")
    return exit_code, report
"""


def test_detector_accepts_armed_shape() -> None:
    assert _apply_arms_composition_budget(_ARMED_FIXTURE, "execute_directives")


@pytest.mark.parametrize(
    "fixture",
    [
        _NONE_BUDGET_FIXTURE,
        _MISSING_BUDGET_KWARG_FIXTURE,
        _NO_FLUSH_FIXTURE,
        _FLUSH_OUTSIDE_FINALLY_FIXTURE,
    ],
    ids=[
        "explicit-none-budget",
        "missing-budget-kwarg",
        "no-flush-at-all",
        "flush-outside-finally",
    ],
)
def test_detector_rejects_regressed_shapes(fixture: str) -> None:
    assert not _apply_arms_composition_budget(fixture, "execute_directives")
