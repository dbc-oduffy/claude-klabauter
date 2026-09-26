"""The ceremony budget is a one-directional ratchet.

`ipc.CEREMONY_BUDGET_SECS` bounds every `ceremony.*` op end-to-end. This module is
the enforcement the constant's own comment block names: it fails on any edit that
raises the ceiling, admits a widening override row for a ceremony op, or lets the
`COORDINATOR_DISPATCH_TIMEOUT_SECS` env knob out-resolve the clamp.

Why this test exists rather than trusting the constant: a number in a source file is
a suggestion to the next reader. The failure mode is not malice, it is a plausible
local argument -- "my op is structurally different", "its cost scales with input",
"the box was busy" -- and that argument has already won once here. The 150.0s
`ceremony.scoped_git_commit` row this budget revoked was added with an honest
measurement attached (three trials, 53 git spawns, a 40.9s worst sample) and a
reasonable-sounding sizing rule, and it still amounted to a per-op cap 300x the
500ms end-to-end target of the ceremony that invoked it. The measurement was not
the problem; treating a measurement as a budget was. So the ratchet is written to
be unarguable at the point of edit: PINNED_CEILING below is a second, independent
copy of the number, and lifting the constant without lifting this one fails the
suite. Lifting both is a deliberate, reviewable act with a PM ruling behind it
(DR-348), not a quiet retune.

Negative spec -- what this module does NOT assert, deliberately:
  - It does NOT assert any ceremony op COMPLETES within the budget. That is a
    latency property, measured by the per-op KPI tests
    (e.g. `test_wsc_tail_parity.py::test_kpi_wsc_tail_blocking_path_under_2s`).
    This module only pins the CEILING that bounds them.
  - It does NOT forbid a ceremony op resolving BELOW the budget. Narrowing is the
    permitted direction; an operator dialling the env knob down for a fast-fail run
    is a supported use, and asserting equality would break it.
  - It does NOT pin the non-ceremony ceiling. The global runaway guard is a separate
    question with a separate rationale (DEC-2), and conflating them here would make
    an unrelated 30s retune read as a ceremony-budget breach; its high-water mark
    lives in `test_ipc_per_request_state.py`. What this module DOES now assert about
    non-ceremony ops is narrower and belongs here: that the same env knob cannot
    WIDEN them either. One variable resolves both lanes, so proving it cannot widen
    ceremony while it can still widen everything else proves less than it looks.

Spec backlink: docs/decisions/DR-348-the-ceremony-budget-is-a-ratchet.md
Load-norm backlink: docs/wiki/machine-load-norm.md -- ">2s for any process is
FORBIDDEN"; the budget is that ruling expressed at the dispatch seam.
"""
from __future__ import annotations

import pytest

from coordinator_core import ipc
from coordinator_core.op_scopes import OP_KEY_SCOPE

PINNED_CEILING_SECS = 2.0

CEREMONY_OPS = sorted(op for op in OP_KEY_SCOPE if ipc.is_ceremony_method(op))


def test_budget_constant_is_at_or_below_the_pinned_ceiling():
    assert ipc.CEREMONY_BUDGET_SECS <= PINNED_CEILING_SECS, (
        f"ceremony budget raised to {ipc.CEREMONY_BUDGET_SECS}s, above the pinned "
        f"{PINNED_CEILING_SECS}s ceiling. The budget ratchets DOWN only. An op that "
        f"needs a wider cap is an op with a defect -- make it cheaper (fewer spawns, "
        f"batched git, a warm path). Raising this pair requires a PM ruling recorded "
        f"in DR-348."
    )


def test_the_keying_table_actually_holds_ceremony_ops():
    """Guards the guard: an empty CEREMONY_OPS would make the sweeps below vacuous."""
    assert CEREMONY_OPS, (
        "no ceremony.* ops found in OP_KEY_SCOPE -- the parametrized sweeps in this "
        "module would silently pass over an empty set. Either the keying table's "
        "naming changed or ipc.is_ceremony_method no longer matches it."
    )


@pytest.mark.parametrize("method", CEREMONY_OPS)
def test_every_ceremony_op_resolves_within_budget(method):
    assert ipc._timeout_for(method) <= PINNED_CEILING_SECS


def test_an_unlisted_future_ceremony_op_is_bounded_by_construction():
    assert ipc._timeout_for("ceremony.not_yet_written") <= PINNED_CEILING_SECS


def test_env_knob_cannot_widen_a_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "9999")
    assert ipc._timeout_for("ceremony.scoped_git_commit") <= PINNED_CEILING_SECS
    assert ipc._timeout_for("ceremony.wsc_tail") <= PINNED_CEILING_SECS


def test_env_knob_may_still_narrow_a_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.5")
    assert ipc._timeout_for("ceremony.wsc_tail") == pytest.approx(0.5)


def test_no_override_row_widens_a_ceremony_op():
    offenders = {
        op: secs
        for op, secs in ipc._OP_TIMEOUT_OVERRIDES.items()
        if ipc.is_ceremony_method(op) and secs > PINNED_CEILING_SECS
    }
    assert not offenders, (
        f"ceremony override rows exceed the budget: {offenders}. The clamp in "
        f"_timeout_for makes such a row inert, so it is not a live regression -- it "
        f"is a lie left in the table for the next reader to act on. Delete it."
    )


def test_an_injected_widening_row_is_still_clamped(monkeypatch):
    monkeypatch.setitem(ipc._OP_TIMEOUT_OVERRIDES, "ceremony.scoped_git_commit", 150.0)
    assert ipc._timeout_for("ceremony.scoped_git_commit") <= PINNED_CEILING_SECS


def test_non_ceremony_ops_are_untouched_by_the_clamp():
    assert ipc._timeout_for("ping") == ipc._resolve_dispatch_timeout_secs()


#: PINNED_CEILING_SECS: importing `ipc._CEREMONY_PACKAGE_PREFIX` would make this
PINNED_CEREMONY_PACKAGE = "coordinator_core.ops.ceremony."


def _ceremony_package_ops():
    """Ops whose implementation lives in the ceremony package, read from the static map.

    Read live rather than hardcoded, for the same reason as CEREMONY_OPS: a new op
    added to the package is covered the moment it lands.
    """
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    return sorted(
        op
        for op, module in OP_MODULE_MAP.items()
        if isinstance(module, str) and module.startswith(PINNED_CEREMONY_PACKAGE)
    )


def test_ops_implemented_in_the_ceremony_package_are_ceremony_ops():
    package_ops = _ceremony_package_ops()
    assert package_ops, (
        "no ops found under " + PINNED_CEREMONY_PACKAGE + " in OP_MODULE_MAP -- "
        "either the package moved or the map's shape changed, and this sweep is "
        "now silently passing over an empty set."
    )
    for op in package_ops:
        assert ipc.is_ceremony_method(op), (
            f"{op} is implemented in the ceremony package but is not recognised as "
            f"a ceremony op -- the rename bypass is open again."
        )
        assert ipc._timeout_for(op) <= PINNED_CEILING_SECS


def test_ceremony_package_membership_survives_a_cold_interpreter():
    """The package signal alone is NOT enough, and this is what proves it.

    `ipc._owning_module_is_ceremony` reads two tables that must already be
    resident and is forbidden from importing (`_registry_map` costs a measured
    470ms, a DR-344 breach on the dispatch hot path). So it answers False in a
    fresh interpreter, and an op whose ONLY membership signal is its owning
    module gets 30s cold and 2s warm -- the same op budgeted or not depending on
    what else the process happened to import, which is verbatim the silent
    escape `is_ceremony_method`'s docstring says the explicit table exists to
    make impossible.

    `test_ops_implemented_in_the_ceremony_package_are_ceremony_ops` cannot catch
    that: it reads `OP_MODULE_MAP` to build its roster, so the map is loaded by
    the time it asks, and the backstop answers True for exactly the op that is
    broken cold. This asserts the IMPORT-FREE signals instead -- the name prefix
    or the alias table -- which are the two that are true everywhere.

    Measured 2026-08-26 on `push.outstanding`: 30.0s cold, 2.0s after
    `import coordinator_core.ops._registry_map`. It was resolved by moving the
    op out of the ceremony package (a remote leg cannot live under a 2s wall
    ceiling; see that module's docstring), not by an alias row -- either
    resolution satisfies this test, which is the point: it pins determinism, not
    which budget an op lands in.
    """
    for op in _ceremony_package_ops():
        assert (
            op.startswith("ceremony.") or op in ipc._CEREMONY_PACKAGE_ALIASES
        ), (
            f"{op} lives in {PINNED_CEREMONY_PACKAGE} but is recognised as ceremony "
            f"ONLY by ipc._owning_module_is_ceremony, which cannot import and so "
            f"answers False in a cold interpreter. Its budget is therefore "
            f"load-order dependent: 30s cold, {PINNED_CEILING_SECS}s warm. Fix it in "
            f"one of the two import-free directions -- add a row to "
            f"_CEREMONY_PACKAGE_ALIASES if the op belongs inside the budget, or move "
            f"the implementation out of the ceremony package if it does not (a remote "
            f"leg does not). Do not leave it resting on the backstop."
        )


def test_a_renamed_ceremony_op_does_not_escape_the_budget(monkeypatch):
    def _renamed_handler(params, ctx=None, repo_root=None):
        return {}

    _renamed_handler.__module__ = PINNED_CEREMONY_PACKAGE + "scoped_git_commit"
    monkeypatch.setitem(ipc._REGISTRY, "commit.scoped_git_commit", _renamed_handler)
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "9999")

    assert ipc.is_ceremony_method("commit.scoped_git_commit")
    assert ipc._timeout_for("commit.scoped_git_commit") <= PINNED_CEILING_SECS


def test_membership_is_a_union_not_an_intersection():
    assert ipc.is_ceremony_method("ceremony.init_anchor_injection_state")
    assert ipc._timeout_for("ceremony.init_anchor_injection_state") <= PINNED_CEILING_SECS


def test_membership_resolution_never_imports_the_ops_package(monkeypatch):
    import importlib

    calls = []
    real_import_module = importlib.import_module

    def _tracking_import(name, *args, **kwargs):
        calls.append(name)
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _tracking_import)
    ipc.is_ceremony_method("some.unregistered.op")
    ipc.is_ceremony_method("ceremony.wsc_tail")
    ipc._timeout_for("some.unregistered.op")

    assert not calls, f"membership resolution imported modules: {calls}"


def test_env_knob_cannot_widen_a_non_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "9999")
    baseline = ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("ping") <= baseline
    assert ipc._timeout_for("session.boot_sweep") <= baseline
    assert ipc._timeout_for("no.such.op") <= baseline


def test_env_knob_may_still_narrow_a_non_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.5")
    assert ipc._timeout_for("ping") == pytest.approx(0.5)


def test_the_two_ceilings_do_not_collapse_into_one():
    assert ipc.CEREMONY_BUDGET_SECS < ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("ceremony.wsc_tail") < ipc._timeout_for("ping")


def test_dump_op_timeouts_projects_the_budget_to_external_callers():
    from coordinator_core.invoke.__main__ import _dump_op_timeouts

    payload = _dump_op_timeouts()
    assert payload["__ceremony_budget__"] <= PINNED_CEILING_SECS
    for method in CEREMONY_OPS:
        assert method in payload, (
            f"{method} absent from --dump-op-timeouts; an external caller would "
            f"fall back to __default__ and size its ceiling for the wrong budget."
        )
        assert payload[method] <= PINNED_CEILING_SECS
