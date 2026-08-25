"""AC9/AC10 pin (docs/plans/2026-08-15-composition-invocation-budgets.md,
chunk C10): both PARTIAL_MUTATION ladders — `contract.apply_base.
execute_directives` and `ceremony_common.apply_halt`'s three budget
primitives — wire `composition_budget.CompositionBudget` at safe
boundaries only. This file asserts:

    - the breach MESSAGE SHAPE (`BudgetBreach.__str__`'s register-
      conforming contract, § composition_budget.py)
    - breach-before-first-mutation and breach-after-last-mutation on BOTH
      ladders
    - a breach NEVER reaches `_run_compensators`
    - no mid-mutation abort path exists (a breach mid-loop is observed,
      never aborts a directive already dispatching)

`apply_halt`'s three primitives (`budget_check_pre_mutation`,
`budget_check_post_mutation`, `budget_advisory_mid_directive`) are tested
directly, standing in for a caller's own loop (workday_complete /
workweek_complete / workstream_complete) per this chunk's write-scope
limit to the two ladder files plus this test — see the plan chunk's own
scope note: those three `apply.py` files author their own loop shapes and
are not touched here.

Spec backlink: docs/plans/2026-08-15-composition-invocation-budgets.md § C10
               coordinator_core/composition_budget.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.ceremony_common import apply_halt
from coordinator_core.composition_budget import CompositionBudget, FAIL_LOUD
from coordinator_core.contract import apply_base


def _budget(*, disposition: str = FAIL_LOUD, breached: bool = True) -> CompositionBudget:
    """A `CompositionBudget` whose elapsed ceiling is already breached
    (or not) at construction time, via an injected clock — no real sleep
    needed. `max_invocations=None` so only the elapsed ceiling is live."""
    ticks = iter([0.0] + ([100.0] if breached else [0.0]) * 50)

    def clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 100.0 if breached else 0.0

    return CompositionBudget(
        composition_id="test-composition-1",
        aggregate_elapsed_budget=1.0,
        disposition=disposition,
        clock=clock,
    )


def _ok_handler(args: list[str], repo_root: Path) -> dict[str, Any]:
    return {"ok": True}


def _compensator_spy(calls: list[str]):
    def _compensator(directive: dict[str, Any], repo_root: Path, detail: Any) -> None:
        calls.append(directive["id"])

    return _compensator


# ---------------------------------------------------------------------------
# Breach message shape
# ---------------------------------------------------------------------------


class TestBreachMessageShape:
    def test_apply_base_pre_mutation_breach_message_shape(self, tmp_path: Path) -> None:
        budget = _budget(breached=True)
        rc, report = apply_base.execute_directives(
            directives=[{"id": "d1", "cli": "noop", "args": []}],
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": _ok_handler},
            composition_budget=budget,
        )
        assert rc == apply_base.APPLY_EXIT_CLAIM_DENIED
        assert "budget_breach" in report
        msg = report["budget_breach"]
        assert "composition budget breach" in msg
        assert "composition='test-composition-1'" in msg
        assert "unit='pre_mutation'" in msg

    def test_apply_halt_pre_mutation_breach_message_shape(self) -> None:
        budget = _budget(breached=True)
        msg = apply_halt.budget_check_pre_mutation(budget)
        assert msg is not None
        assert "composition budget breach" in msg
        assert "composition='test-composition-1'" in msg
        assert "unit='pre_mutation'" in msg


# ---------------------------------------------------------------------------
# apply_base ladder: breach-before-first-mutation / breach-after-last
# ---------------------------------------------------------------------------


class TestApplyBaseBoundaries:
    def test_breach_before_first_mutation_never_dispatches_a_handler(
        self, tmp_path: Path
    ) -> None:
        dispatched: list[str] = []

        def handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            dispatched.append("called")
            return {"ok": True}

        budget = _budget(breached=True)
        rc, report = apply_base.execute_directives(
            directives=[{"id": "d1", "cli": "noop", "args": []}],
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": handler},
            composition_budget=budget,
        )
        assert rc == apply_base.APPLY_EXIT_CLAIM_DENIED
        assert report["landed"] == []
        assert dispatched == []

    def test_breach_after_last_mutation_keeps_rc_ok_and_does_not_abort(
        self, tmp_path: Path
    ) -> None:
        """The budget breaches only once elapsed time has passed the
        ceiling -- by construction (clock ticks 0.0 at start, then jumps
        to 100.0 on every subsequent read), the pre-mutation check (first
        clock read after __post_init__'s own read) is still within
        budget, every directive dispatches normally, and the breach is
        observed only at the post-loop boundary."""
        clock_calls = {"n": 0}

        def clock() -> float:
            # First call is __post_init__'s _start read (0.0). Every
            # call after that returns 0.0 until the post-mutation
            # boundary check, which is the LAST elapsed_secs() read this
            # test cares about -- simulate "still fast" for pre-mutation
            # and mid-directive checks, then "slow" once, at the very end.
            clock_calls["n"] += 1
            # start(0) + pre-mutation elapsed_secs(1) + mid-directive
            # elapsed_secs(2..N) all read fast; post-mutation boundary is
            # the final read this test triggers.
            if clock_calls["n"] <= 3:
                return 0.0
            return 100.0

        budget = CompositionBudget(
            composition_id="test-composition-2",
            aggregate_elapsed_budget=1.0,
            disposition=FAIL_LOUD,
            clock=clock,
        )
        rc, report = apply_base.execute_directives(
            directives=[{"id": "d1", "cli": "noop", "args": []}],
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": _ok_handler},
            composition_budget=budget,
        )
        assert rc == apply_base.APPLY_EXIT_OK
        assert report["landed"] == ["d1"]
        assert "budget_breach" in report
        assert "unit='post_mutation'" in report["budget_breach"]

    def test_breach_never_reaches_compensators(self, tmp_path: Path) -> None:
        comp_calls: list[str] = []
        budget = _budget(breached=True)
        rc, report = apply_base.execute_directives(
            directives=[{"id": "d1", "cli": "noop", "args": []}],
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": _ok_handler},
            composition_budget=budget,
            compensators={"d1": _compensator_spy(comp_calls)},
        )
        assert rc == apply_base.APPLY_EXIT_CLAIM_DENIED
        assert comp_calls == []
        assert "compensation" not in report

    def test_compensator_outcome_is_not_masked_by_budget_instrumentation(
        self, tmp_path: Path
    ) -> None:
        """2026-08-15 regression pin (root-caused off `test_apply_base.
        py::TestAGenuineFailureStillCompensates::test_a_genuine_failure_
        still_compensates`, coordinator_core/baton_assemble): a compensator
        is itself a caller-supplied callable that may resolve session
        identity from `os.environ` and shell out, same as the per-directive
        `handler` dispatch it reacts to (§ apply_base module docstring
        "SESSION IDENTITY SHAPE"). `_run_compensators` must mirror the
        active `session_identity()` scope into `os.environ` for the
        duration of EACH compensator call, exactly as `execute_directives`'
        own loop mirrors it for each handler call -- otherwise a
        compensator that behaves differently with/without ambient identity
        (e.g. re-rendering the same artifact a handler minted, which
        stamps an identity-derived field only when the identity is
        visible) silently diverges from what it produced moments earlier,
        while `_run_compensators`' own try/except still reports
        `succeeded: True` because the compensator itself never raised. A
        real compensator FAILURE must also still surface honestly --
        composition-budget instrumentation errors are isolated (never take
        down a close), but a compensator's own outcome is never budget
        instrumentation and must never be swallowed or altered by it.

        This test never sets `composition_budget` at all -- the defect was
        in the un-wired reaction path itself (`_run_compensators`), not
        conditional on a budget being present. A present-but-unbreached
        budget is layered in by `test_breach_never_reaches_compensators`
        above; this pin is about the mirror wrap alone."""
        seen: dict[str, Any] = {}

        def ok_handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            return {"ok": True}

        def fail_handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            raise RuntimeError("genuine directive failure")

        def compensator(
            directive: dict[str, Any], repo_root: Path, detail: Any
        ) -> None:
            seen["env"] = os.environ.get("COORDINATOR_SESSION_ID")

        directives = [
            {"id": "d1", "cli": "noop", "args": []},
            {"id": "d2", "cli": "fail", "args": [], "depends_on": "d1"},
        ]
        with apply_base.session_identity("sess-compensator-pin"):
            rc, report = apply_base.execute_directives(
                directives=directives,
                judgment_points=[],
                repo_root=tmp_path,
                dispatch_table={"noop": ok_handler, "fail": fail_handler},
                compensators={"d1": compensator},
            )

        assert rc == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": True}
        ]
        assert seen["env"] == "sess-compensator-pin"

    def test_a_declining_compensator_is_not_recorded_as_a_successful_rollback(
        self, tmp_path: Path
    ) -> None:
        """Downstream of ada42cb429f2 (`_run_compensators` return-value
        contract): a compensator that runs, decides not to act, and signals
        that via an explicit `False` return must be distinguished from both
        a genuine success (`None`, today's universal registered-compensator
        return) and a genuine failure (a raise) -- never folded into
        `succeeded: True` merely because it did not raise."""

        def ok_handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            return {"ok": True}

        def fail_handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            raise RuntimeError("genuine directive failure")

        def declining_compensator(
            directive: dict[str, Any], repo_root: Path, detail: Any
        ) -> bool:
            return False

        directives = [
            {"id": "d1", "cli": "noop", "args": []},
            {"id": "d2", "cli": "fail", "args": [], "depends_on": "d1"},
        ]
        rc, report = apply_base.execute_directives(
            directives=directives,
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": ok_handler, "fail": fail_handler},
            compensators={"d1": declining_compensator},
        )

        assert rc == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": False, "declined": True}
        ]

    def test_no_mid_mutation_abort_all_directives_still_dispatch(
        self, tmp_path: Path
    ) -> None:
        """A budget that breaches partway through (after the first
        directive's mid-directive advisory) must NOT abort the remaining
        directives -- advisory_check never raises, never affects control
        flow. Every directive dispatches and lands."""
        dispatched: list[str] = []

        def handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            dispatched.append(args[0] if args else "?")
            return {"ok": True}

        directives = [
            {"id": "d1", "cli": "noop", "args": ["one"]},
            {"id": "d2", "cli": "noop", "args": ["two"]},
            {"id": "d3", "cli": "noop", "args": ["three"]},
        ]
        # Budget with an already-breached elapsed ceiling but
        # skip-and-surface disposition, wired as an ADVISORY-only budget
        # via a max_invocations ceiling that never trips the pre-mutation
        # boundary (aggregate_elapsed_budget=None) so we isolate: does a
        # mid-directive advisory breach abort dispatch? It must not.
        budget = CompositionBudget(
            composition_id="test-composition-3",
            aggregate_elapsed_budget=None,
            max_invocations=1,
            disposition=FAIL_LOUD,
        )
        rc, report = apply_base.execute_directives(
            directives=directives,
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": handler},
            composition_budget=budget,
        )
        assert rc == apply_base.APPLY_EXIT_OK
        assert dispatched == ["one", "two", "three"]
        assert report["landed"] == ["d1", "d2", "d3"]

    def test_none_composition_budget_is_byte_identical_to_omitted(
        self, tmp_path: Path
    ) -> None:
        directives = [{"id": "d1", "cli": "noop", "args": []}]
        rc_a, report_a = apply_base.execute_directives(
            directives=directives,
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": _ok_handler},
        )
        rc_b, report_b = apply_base.execute_directives(
            directives=directives,
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": _ok_handler},
            composition_budget=None,
        )
        assert rc_a == rc_b
        assert report_a == report_b
        assert "budget_breach" not in report_a


# ---------------------------------------------------------------------------
# apply_halt ladder: breach-before-first-mutation / breach-after-last,
# exercised through the three call-shaped primitives a ceremony
# assembler's own loop would invoke.
# ---------------------------------------------------------------------------


class TestApplyHaltBoundaries:
    def test_pre_mutation_breach_reports_directive_failed_shape(self) -> None:
        budget = _budget(breached=True)
        msg = apply_halt.budget_check_pre_mutation(budget)
        assert msg is not None
        exit_codes = apply_halt.CEREMONY_HALT_EXIT_CODES
        assert exit_codes["DIRECTIVE_FAILED"] == 2
        # A caller wires: `if msg: return DIRECTIVE_FAILED, {"landed": [], "budget_breach": msg}`

    def test_pre_mutation_no_breach_returns_none(self) -> None:
        budget = _budget(disposition=FAIL_LOUD, breached=False)
        assert apply_halt.budget_check_pre_mutation(budget) is None

    def test_post_mutation_breach_reports_message_but_caller_keeps_rc(self) -> None:
        budget = _budget(breached=True)
        msg = apply_halt.budget_check_post_mutation(budget)
        assert msg is not None
        assert "unit='post_mutation'" in msg
        # apply_halt itself never returns an rc here -- a caller invoking
        # this after its own loop finished successfully keeps whatever rc
        # that loop already computed (never PARTIAL_MUTATION/DIRECTIVE_FAILED).

    def test_post_mutation_no_breach_returns_none_and_prints_nothing(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        budget = _budget(disposition=FAIL_LOUD, breached=False)
        assert apply_halt.budget_check_post_mutation(budget) is None
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_mid_directive_advisory_never_raises_and_never_aborts(self) -> None:
        budget = _budget(breached=True)  # already past the elapsed ceiling
        # Must not raise, regardless of disposition=FAIL_LOUD.
        apply_halt.budget_advisory_mid_directive(budget, "some-directive")
        assert budget.invocation_count == 1
        assert "some-directive" in budget.breached_units

    def test_mid_directive_advisory_none_budget_is_a_no_op(self) -> None:
        # Must not raise when no budget is wired at all.
        apply_halt.budget_advisory_mid_directive(None, "some-directive")

    def test_breach_never_reaches_a_compensation_pass(self) -> None:
        """apply_halt owns no loop/compensator concept of its own -- this
        pins the structural guarantee instead: `budget_check_pre_mutation`
        and `budget_check_post_mutation` return a message/None and never
        themselves invoke anything resembling `_run_compensators` (which
        does not exist in this module at all, by its own negative-spec)."""
        assert not hasattr(apply_halt, "_run_compensators")
        assert not hasattr(apply_halt, "compensators")


# ---------------------------------------------------------------------------
# Instrumentation-error isolation: an exception from an injected on_count
# (or any budget-internal machinery other than BudgetBreach) must not take
# the run down.
# ---------------------------------------------------------------------------


class TestInstrumentationErrorIsolation:
    def test_apply_base_on_count_exception_does_not_abort_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        def broken_on_count(unit: str, total: int) -> None:
            raise RuntimeError("sink is down")

        budget = CompositionBudget(
            composition_id="test-composition-4",
            on_count=broken_on_count,
        )
        rc, report = apply_base.execute_directives(
            directives=[{"id": "d1", "cli": "noop", "args": []}],
            judgment_points=[],
            repo_root=tmp_path,
            dispatch_table={"noop": _ok_handler},
            composition_budget=budget,
        )
        assert rc == apply_base.APPLY_EXIT_OK
        assert report["landed"] == ["d1"]
        captured = capsys.readouterr()
        assert "instrumentation error" in captured.err

    def test_apply_halt_advisory_on_count_exception_does_not_raise(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        def broken_on_count(unit: str, total: int) -> None:
            raise RuntimeError("sink is down")

        budget = CompositionBudget(
            composition_id="test-composition-5",
            on_count=broken_on_count,
        )
        apply_halt.budget_advisory_mid_directive(budget, "d1")
        captured = capsys.readouterr()
        assert "instrumentation error" in captured.err
