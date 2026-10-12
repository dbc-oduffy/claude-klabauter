"""run_stages: one terminal ledger row per step, gates, rules, budget, admission."""
from __future__ import annotations

import pytest

from coordinator_core.composition_budget import CompositionBudget
from coordinator_core.contract import apply_base
from coordinator_core.contract.apply_base import UnrecognizedDirective
from coordinator_core.workday_start import ledger, runner
from coordinator_core.workday_start.contract import StepSpec
from coordinator_core.workday_start.runner import BudgetRefused, ChildOutcome, rule_child, run_stages


@pytest.fixture(autouse=True)
def _demo_row(monkeypatch):
    monkeypatch.setattr(apply_base, "ASSEMBLER_DISPATCHABLE", {"demo": frozenset({"alpha", "beta", "gamma"})})


def _step(id_, *, target="alpha", kind="cli", gates=False, on_exit=None, cost="engine", satisfied=False):
    return StepSpec(
        id=id_, stage="s", kind=kind, target=target, args=(), depends_on=None,
        already_satisfied=satisfied, detail="", heading=id_, cost_class=cost,
        gates_next=gates, on_exit=on_exit or {},
    )


class Fake:
    def __init__(self, codes=None, raises=()):
        self.codes = codes or {}
        self.raises = set(raises)
        self.started: list[str] = []
        self.rule_argvs: list[list[str]] = []

    def table(self):
        def run(step, *, repo_root, run_dir, deadline_s):
            self.started.append(step.id)
            if step.id in self.raises:
                raise RuntimeError("boom")
            return ChildOutcome(exit_code=self.codes.get(step.id, 0), process_ms=1.0, spawns=1,
                                stderr="bad thing\nmore" if self.codes.get(step.id) else "")

        def rule_run(argv, *, repo_root, deadline_s):
            self.rule_argvs.append(list(argv))
            return ChildOutcome(exit_code=0, process_ms=2.0, spawns=1)

        return {"cli": run, "op": run, "inproc": run, "rule-child": rule_run}


def _run(tmp_path, steps, fake, rules=None, budget=None):
    run_dir = tmp_path / "run"
    results = run_stages(steps, ceremony="demo", repo_root=tmp_path, run_dir=run_dir,
                         dispatch=fake.table(), budget=budget, rules=rules or {})
    return results, ledger.read(run_dir)


def test_one_row_per_step_in_order(tmp_path):
    steps = [_step("a"), _step("b", target="beta"), _step("c", target="gamma")]
    results, rows = _run(tmp_path, steps, Fake())
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert [r.status for r in results] == ["ran"] * 3


def test_raising_step_is_failed_and_next_still_runs(tmp_path):
    fake = Fake(raises={"a"})
    results, rows = _run(tmp_path, [_step("a"), _step("b", target="beta")], fake)
    assert [r["status"] for r in rows] == ["failed", "ran"]
    assert "boom" in rows[0]["reason"]
    assert fake.started == ["a", "b"]


def test_nonzero_exit_without_gate_is_failed_row_with_reason(tmp_path):
    results, rows = _run(tmp_path, [_step("a"), _step("b", target="beta")], Fake(codes={"a": 1}))
    assert rows[0]["status"] == "failed" and rows[0]["exit_code"] == 1
    assert rows[0]["reason"] == "exit 1: bad thing"
    assert rows[1]["status"] == "ran"


def test_gate_without_rule_fails_and_halts_the_rest(tmp_path):
    fake = Fake(codes={"g": 3})
    steps = [_step("g", gates=True, on_exit={2: "r"}), _step("b", target="beta"), _step("c", target="gamma")]
    _, rows = _run(tmp_path, steps, fake)
    assert [r["status"] for r in rows] == ["failed", "halted-by-gate", "halted-by-gate"]
    assert rows[1]["reason"] == "g"
    assert fake.started == ["g"]


def test_gate_resolved_by_rule_continues(tmp_path):
    fake = Fake(codes={"g": 2})
    steps = [_step("g", gates=True, on_exit={2: "r"}), _step("b", target="beta")]
    _, rows = _run(tmp_path, steps, fake, rules={"r": lambda *a, **k: True})
    assert rows[0]["status"] == "ran" and rows[0]["reason"] == "resolved-by:r"
    assert rows[1]["status"] == "ran"


def test_rule_returning_string_fails_with_that_reason(tmp_path):
    fake = Fake(codes={"g": 2})
    steps = [_step("g", gates=True, on_exit={2: "r"}), _step("b", target="beta")]
    _, rows = _run(tmp_path, steps, fake, rules={"r": lambda *a, **k: "stop-and-file"})
    assert rows[0]["status"] == "failed" and rows[0]["reason"] == "stop-and-file"
    assert rows[1]["status"] == "halted-by-gate"


def test_rule_that_raises_halts(tmp_path):
    def bad(*a, **k):
        raise ValueError("nope")

    fake = Fake(codes={"g": 2})
    steps = [_step("g", gates=True, on_exit={2: "r"}), _step("b", target="beta")]
    _, rows = _run(tmp_path, steps, fake, rules={"r": bad})
    assert rows[0]["status"] == "failed" and "nope" in rows[0]["reason"]
    assert rows[1]["status"] == "halted-by-gate"


def test_gate_that_raises_halts(tmp_path):
    fake = Fake(raises={"g"})
    _, rows = _run(tmp_path, [_step("g", gates=True), _step("b", target="beta")], fake)
    assert [r["status"] for r in rows] == ["failed", "halted-by-gate"]


def test_over_bar_step_is_never_dispatched(tmp_path):
    fake = Fake()
    _, rows = _run(tmp_path, [_step("a", cost="over-bar"), _step("b", target="beta")], fake)
    assert rows[0]["status"] == "not-composed"
    assert fake.started == ["b"]


def test_over_bar_gate_halts(tmp_path):
    fake = Fake()
    _, rows = _run(tmp_path, [_step("g", gates=True, cost="over-bar"), _step("b", target="beta")], fake)
    assert [r["status"] for r in rows] == ["not-composed", "halted-by-gate"]
    assert fake.started == []


def test_already_satisfied_is_never_dispatched(tmp_path):
    fake = Fake()
    _, rows = _run(tmp_path, [_step("a", satisfied=True)], fake)
    assert rows[0]["status"] == "ran" and rows[0]["reason"] == "already-satisfied"
    assert fake.started == []


def test_rule_child_gets_its_own_row(tmp_path):
    def rule(step, result, *, repo_root, run_dir):
        rule_child(["git", "status"], cost_class="engine")
        return True

    fake = Fake(codes={"g": 2})
    steps = [_step("g", gates=True, on_exit={2: "r"})]
    _, rows = _run(tmp_path, steps, fake, rules={"r": rule})
    assert [r["id"] for r in rows] == ["g:rule:r", "g"]
    assert rows[0]["reason"] == "cost-class:engine" and rows[0]["process_ms"] == 2.0
    assert fake.rule_argvs == [["git", "status"]]


def test_rule_child_refused_for_over_bar_and_outside_a_rule(tmp_path):
    with pytest.raises(RuntimeError):
        rule_child(["git"], cost_class="engine")

    def rule(step, result, *, repo_root, run_dir):
        rule_child(["git"], cost_class="over-bar")

    fake = Fake(codes={"g": 2})
    _, rows = _run(tmp_path, [_step("g", gates=True, on_exit={2: "r"})], fake, rules={"r": rule})
    assert rows[0]["status"] == "failed" and "over-bar" in rows[0]["reason"]
    assert fake.rule_argvs == []


def test_budget_refusal_is_not_composed_with_the_dial(tmp_path):
    budget = CompositionBudget(composition_id="t", max_invocations=1)
    fake = Fake()
    _, rows = _run(tmp_path, [_step("a"), _step("b", target="beta")], fake, budget=budget)
    assert rows[0]["status"] == "ran"
    assert rows[1]["status"] == "not-composed" and rows[1]["reason"] == "invocations"
    assert fake.started == ["a"]


def test_budget_refusal_inside_a_rule_raises(tmp_path):
    budget = CompositionBudget(composition_id="t", max_invocations=1)

    def rule(step, result, *, repo_root, run_dir):
        try:
            rule_child(["git"])
        except BudgetRefused:
            return "budget"
        return True

    fake = Fake(codes={"g": 2})
    _, rows = _run(tmp_path, [_step("g", gates=True, on_exit={2: "r"})], fake, rules={"r": rule}, budget=budget)
    assert rows[0]["reason"] == "budget"


def test_each_started_step_is_counted_against_the_budget(tmp_path):
    budget = CompositionBudget(composition_id="t", max_invocations=50)
    _run(tmp_path, [_step("a"), _step("b", target="beta")], Fake(), budget=budget)
    assert budget.invocation_count == 2


def test_bareword_outside_dispatch_row_raises_before_any_child(tmp_path):
    fake = Fake()
    with pytest.raises(UnrecognizedDirective):
        _run(tmp_path, [_step("a"), _step("z", target="zeta")], fake)
    assert fake.started == []
    assert ledger.read(tmp_path / "run") == []


def test_op_and_inproc_kinds_skip_the_dispatch_row(tmp_path):
    fake = Fake()
    steps = [_step("o", target="any.op", kind="op"), _step("i", target="pkg.mod:fn", kind="inproc")]
    _, rows = _run(tmp_path, steps, fake)
    assert [r["status"] for r in rows] == ["ran", "ran"]


def test_ledger_rows_carry_exactly_the_contract_keys(tmp_path):
    from coordinator_core.workday_start.contract import LEDGER_ROW_KEYS

    _, rows = _run(tmp_path, [_step("a")], Fake())
    assert tuple(rows[0]) == LEDGER_ROW_KEYS


def test_cold_measure_reports_exit_and_output(tmp_path):
    import sys

    out = runner._measure_cold(
        [sys.executable, "-c", "print('hi')"], repo_root=tmp_path, deadline_s=30.0, io_dir=tmp_path
    )
    assert out.exit_code == 0 and out.stdout.strip() == "hi"
    assert out.process_ms is not None and out.spawns and out.spawns >= 1


def test_source_names_no_ceremony():
    for mod in (runner, ledger):
        text = open(mod.__file__, encoding="utf-8").read()
        assert "workday-start" not in text and "workday_start" not in text
