"""
Tests for coordinator_core.composition_budget (docs/plans/2026-08-15-composition-
invocation-budgets.md § C9).

Covers: elapsed-only skip-and-surface parity with percolate's extracted shape,
invocation-count ceiling, fail-loud BudgetBreach message shape (AC9), the
injectable identity channel (env / file / composer-local fallback), the on_count
injection point, and the mid-directive advisory (never-raises) call shape.
"""

from __future__ import annotations

import pytest

from coordinator_core.composition_budget import (
    FAIL_LOUD,
    SKIP_AND_SURFACE,
    BudgetBreach,
    CompositionBudget,
    identity_from_env,
    identity_from_file,
    new_composition_id,
)


class _FakeClock:
    """Deterministic, hand-advanced clock for budget tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def test_no_budget_set_never_breaches():
    clock = _FakeClock()
    budget = CompositionBudget(composition_id="c1", clock=clock)
    clock.advance(1000.0)
    for _ in range(1000):
        budget.record_invocation()
    assert budget.check() is True
    assert budget.breached_units == ()


def test_elapsed_budget_skip_and_surface_parity_with_percolate_shape():
    """§ C9 body: `_budget_ok()` parity -- elapsed strictly greater than the budget breaches,
    elapsed equal to the budget does not (matches run_entrypoint_gate's `<=` comparison)."""
    clock = _FakeClock()
    budget = CompositionBudget(composition_id="c1", aggregate_elapsed_budget=10.0, clock=clock)

    clock.advance(10.0)
    assert budget.check() is True  # exactly at budget: still ok, mirrors `<=`

    clock.advance(0.001)
    assert budget.check() is False  # now over
    assert budget.breached_units == ("elapsed",)
    # skip-and-surface: never raises
    assert budget.disposition == SKIP_AND_SURFACE


def test_invocation_count_ceiling_is_independent_of_elapsed():
    clock = _FakeClock()
    budget = CompositionBudget(composition_id="c1", max_invocations=3, clock=clock)
    budget.record_invocation()
    budget.record_invocation()
    assert budget.check() is True
    budget.record_invocation()
    assert budget.check() is False
    assert budget.breached_units == ("invocations",)


def test_fail_loud_raises_budget_breach_with_named_composition_fanout_unit_and_count():
    """AC9: breach fails loudly with a message naming the composition, the fan-out unit,
    and the count -- asserted here on the message shape."""
    clock = _FakeClock()
    budget = CompositionBudget(
        composition_id="wsc-close-42",
        max_invocations=2,
        disposition=FAIL_LOUD,
        clock=clock,
    )
    budget.record_invocation()
    budget.record_invocation()

    with pytest.raises(BudgetBreach) as excinfo:
        budget.check(unit="review-coverage-gate")

    exc = excinfo.value
    assert exc.composition_id == "wsc-close-42"
    assert exc.unit == "review-coverage-gate"
    assert exc.invocation_count == 2
    message = str(exc)
    assert "wsc-close-42" in message
    assert "review-coverage-gate" in message
    assert "invocations=2" in message
    assert budget.breached_units == ("review-coverage-gate",)


def test_fail_loud_default_unit_names_which_ceiling_breached():
    clock = _FakeClock()
    budget = CompositionBudget(
        composition_id="c1", aggregate_elapsed_budget=5.0, disposition=FAIL_LOUD, clock=clock
    )
    clock.advance(6.0)
    with pytest.raises(BudgetBreach) as excinfo:
        budget.check()
    assert excinfo.value.unit == "elapsed"


def test_invalid_disposition_rejected_at_construction():
    with pytest.raises(ValueError):
        CompositionBudget(composition_id="c1", disposition="abort-now")


def test_advisory_check_never_raises_regardless_of_disposition():
    clock = _FakeClock()
    budget = CompositionBudget(
        composition_id="c1", max_invocations=1, disposition=FAIL_LOUD, clock=clock
    )
    budget.record_invocation()
    # advisory_check must not raise even though disposition is fail-loud
    assert budget.advisory_check() is False
    assert budget.breached_units == ("invocations",)
    # a later boundary check() still raises -- advisory is observation-only, not a
    # substitute for the boundary check
    with pytest.raises(BudgetBreach):
        budget.check()


def test_on_count_injection_point_receives_unit_and_running_total():
    calls = []
    budget = CompositionBudget(
        composition_id="c1", on_count=lambda unit, total: calls.append((unit, total))
    )
    budget.record_invocation("git-spawn")
    budget.record_invocation("git-spawn", count=3)
    assert calls == [("git-spawn", 1), ("git-spawn", 4)]
    assert budget.invocation_count == 4


def test_on_count_not_called_by_check_or_advisory_check():
    calls = []
    budget = CompositionBudget(
        composition_id="c1",
        max_invocations=1,
        on_count=lambda unit, total: calls.append((unit, total)),
    )
    budget.record_invocation()
    assert calls == [("invocation", 1)]
    budget.check()
    budget.advisory_check()
    assert calls == [("invocation", 1)]  # unchanged -- check()/advisory_check() are read-only


def test_new_composition_id_is_unique_and_pid_prefixed():
    import os

    id1 = new_composition_id()
    id2 = new_composition_id()
    assert id1 != id2
    assert id1.startswith(f"{os.getpid()}-")


def test_identity_from_env_reads_and_falls_back_to_none(monkeypatch):
    monkeypatch.delenv("COORDINATOR_COMPOSITION_ID", raising=False)
    assert identity_from_env() is None

    monkeypatch.setenv("COORDINATOR_COMPOSITION_ID", "propagated-id")
    assert identity_from_env() == "propagated-id"

    monkeypatch.setenv("MY_CUSTOM_VAR", "custom-id")
    assert identity_from_env("MY_CUSTOM_VAR") == "custom-id"


def test_identity_from_file_reads_strips_and_handles_absence(tmp_path):
    missing = tmp_path / "absent.txt"
    assert identity_from_file(missing) is None

    present = tmp_path / "id.txt"
    present.write_text("  file-composition-id  \n", encoding="utf-8")
    assert identity_from_file(present) == "file-composition-id"

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert identity_from_file(empty) is None


def test_for_composition_uses_resolver_when_it_returns_a_value():
    budget = CompositionBudget.for_composition(identity_resolver=lambda: "env-supplied-id")
    assert budget.composition_id == "env-supplied-id"


def test_for_composition_falls_back_to_new_id_when_resolver_returns_none():
    budget = CompositionBudget.for_composition(identity_resolver=lambda: None)
    assert budget.composition_id  # non-empty, minted
    assert "-" in budget.composition_id


def test_for_composition_falls_back_to_new_id_when_no_resolver_supplied():
    budget = CompositionBudget.for_composition()
    assert budget.composition_id


def test_breached_units_is_append_only_and_never_cleared():
    clock = _FakeClock()
    budget = CompositionBudget(composition_id="c1", max_invocations=1, clock=clock)
    budget.record_invocation()
    budget.check("first")
    budget.check("second")
    assert budget.breached_units == ("first", "second")


class TestFleetDialsAreUntouchedByTheFactLayerPlan:
    """AC8, plan 2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.

    `fl-core-04` arms its OWN dial (X) in
    `coordinator_core/session/session_facts_budget.py` and must leave DR-325's
    two fleet dials byte-identical. The plan never edits
    `composition_budget.py`; this assertion is the mechanical proof, and it
    lives here rather than there for the same reason X does — the check
    belongs beside the constants it pins, not beside the new dial.
    """

    #: DR-325's armed values, docs/research/2026-08-18-composition-budget-armed-values.md.
    PRE_PLAN_FLEET_AGGREGATE_ELAPSED_BUDGET = 1200.0
    PRE_PLAN_FLEET_MAX_INVOCATIONS = 110

    def test_fleet_aggregate_elapsed_budget_is_unchanged(self):
        from coordinator_core.composition_budget import FLEET_AGGREGATE_ELAPSED_BUDGET

        assert FLEET_AGGREGATE_ELAPSED_BUDGET == self.PRE_PLAN_FLEET_AGGREGATE_ELAPSED_BUDGET

    def test_fleet_max_invocations_is_unchanged(self):
        from coordinator_core.composition_budget import FLEET_MAX_INVOCATIONS

        assert FLEET_MAX_INVOCATIONS == self.PRE_PLAN_FLEET_MAX_INVOCATIONS

    def test_the_fact_layer_dial_is_a_separate_constant_in_a_separate_module(self):
        """X must not have been added beside the fleet dials — the whole
        argument of the plan's "Where X lives" section."""
        from coordinator_core import composition_budget
        from coordinator_core.session import session_facts_budget

        assert not hasattr(
            composition_budget, "FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET"
        )
        assert hasattr(session_facts_budget, "FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET")
