"""run_stages: one terminal ledger row per step, gates, rules, budget, admission."""
from __future__ import annotations

import dataclasses
import threading

import pytest

from coordinator_core.composition_budget import CompositionBudget
from coordinator_core.contract import apply_base
from coordinator_core.contract.apply_base import UnrecognizedDirective
from coordinator_core.ceremony_common.run import ledger, runner
from coordinator_core.ceremony_common.run.contract import StepSpec
from coordinator_core.ceremony_common.run.runner import BudgetRefused, ChildOutcome, rule_child, run_stages


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
    from coordinator_core.ceremony_common.run.contract import LEDGER_ROW_KEYS

    _, rows = _run(tmp_path, [_step("a")], Fake())
    assert tuple(rows[0]) == LEDGER_ROW_KEYS
    assert LEDGER_ROW_KEYS.index("wall_ms") == LEDGER_ROW_KEYS.index("spawns") + 1


def test_every_dispatched_row_carries_a_float_wall_ms_the_runner_measured(tmp_path):
    import time

    fake = Fake()
    table = fake.table()
    inner = table["cli"]

    def slow(step, *, repo_root, run_dir, deadline_s):
        time.sleep(0.05)
        return inner(step, repo_root=repo_root, run_dir=run_dir, deadline_s=deadline_s)

    results = run_stages(
        [_step("a"), _step("b", target="beta")], ceremony="demo", repo_root=tmp_path, run_dir=tmp_path / "run",
        dispatch={**table, "cli": slow}, rules={},
    )
    assert all(isinstance(r["wall_ms"], float) and r["wall_ms"] >= 45.0 for r in ledger.read(tmp_path / "run"))
    assert all(isinstance(r.wall_ms, float) for r in results)


def test_wall_ms_survives_the_rule_and_launch_reconstructions(tmp_path):
    fake = Fake(codes={"g": 7})
    steps = [_step("g", gates=True, on_exit={7: "r1"})]
    results, rows = _run(tmp_path, steps, fake, rules={"r1": lambda *a, **k: True})
    assert rows[0]["reason"] == "resolved-by:r1" and isinstance(rows[0]["wall_ms"], float)
    launched = runner._launch_result(runner.dataclasses.replace(results[0], reason="", stdout='{"run_id": "x"}'))
    assert launched.wall_ms == results[0].wall_ms


def test_a_step_that_started_no_child_has_no_wall_ms(tmp_path):
    _, rows = _run(tmp_path, [_step("a", satisfied=True)], Fake())
    assert rows[0]["wall_ms"] is None


def test_cold_measure_reports_exit_and_output(tmp_path):
    import sys

    out = runner._measure_cold(
        [sys.executable, "-c", "print('hi')"], repo_root=tmp_path, deadline_s=30.0
    )
    assert out.exit_code == 0 and out.stdout.strip() == "hi"
    assert out.process_ms is not None and out.spawns and out.spawns >= 1


def _wide(id_, *, target="alpha", cost="engine", gates=False, stdin_from=None, best_effort=False):
    return StepSpec(
        id=id_, stage="s", kind="cli", target=target, args=(), depends_on=None,
        already_satisfied=False, detail="", heading=id_, cost_class=cost, gates_next=gates,
        stdin_from=stdin_from, best_effort=best_effort,
    )


class Recorder:
    def __init__(self, codes=None, stdout=None):
        self.codes, self.out = codes or {}, stdout or {}
        self.started: list[str] = []
        self.stdin: dict[str, str] = {}

    def table(self):
        def run(step, *, repo_root, run_dir, deadline_s, stdin_text=None):
            self.started.append(step.id)
            if stdin_text is not None:
                self.stdin[step.id] = stdin_text
            return ChildOutcome(exit_code=self.codes.get(step.id, 0), process_ms=1.0, spawns=1,
                                stdout=self.out.get(step.id, ""))

        return {"cli": run, "op": run, "inproc": run, "rule-child": lambda *a, **k: ChildOutcome(0)}


def _run_wide(tmp_path, steps, rec, **kw):
    run_dir = tmp_path / "run"
    results = run_stages(steps, ceremony="demo", repo_root=tmp_path, run_dir=run_dir, dispatch=rec.table(), **kw)
    return results, ledger.read(run_dir)


def test_stdin_from_delivers_the_upstream_stdout(tmp_path):
    rec = Recorder(stdout={"a": "payload"})
    _, rows = _run_wide(tmp_path, [_wide("a"), _wide("b", target="beta", stdin_from="a")], rec)
    assert rec.stdin == {"b": "payload"} and rows[1]["status"] == "ran"


def test_stdin_consumer_without_upstream_ran_row_is_not_composed(tmp_path):
    rec = Recorder()
    _, rows = _run_wide(tmp_path, [_wide("b", stdin_from="a")], rec)
    assert rows[0]["status"] == "not-composed" and rows[0]["reason"] == "a"
    assert rec.started == []


def test_stdin_consumer_of_failed_upstream_is_not_composed(tmp_path):
    rec = Recorder(codes={"a": 1})
    _, rows = _run_wide(tmp_path, [_wide("a"), _wide("b", target="beta", stdin_from="a")], rec)
    assert [r["status"] for r in rows] == ["failed", "not-composed"]
    assert rec.started == ["a"]


def test_stdin_consumer_of_over_bar_upstream_is_not_composed(tmp_path):
    rec = Recorder()
    _, rows = _run_wide(tmp_path, [_wide("a", cost="over-bar"), _wide("b", target="beta", stdin_from="a")], rec)
    assert [r["status"] for r in rows] == ["not-composed", "not-composed"]
    assert rows[1]["reason"] == "a" and rec.started == []


def test_failing_best_effort_gate_halts_nothing(tmp_path):
    rec = Recorder(codes={"g": 1})
    steps = [_wide("g", gates=True, best_effort=True), _wide("b", target="beta")]
    _, rows = _run_wide(tmp_path, steps, rec)
    assert [r["status"] for r in rows] == ["failed", "ran"]


def test_suite_step_needs_allow_and_a_ran_grant_row(tmp_path):
    steps = [_wide("grant"), _wide("t", target="beta", cost="suite")]
    rec = Recorder()
    _, rows = _run_wide(tmp_path, steps, rec, allow_suite=False, suite_grant="grant")
    assert rows[1]["status"] == "not-composed" and rows[1]["reason"] == "not-in-launch"
    rec = Recorder()
    _, rows = _run_wide(tmp_path / "x", [_wide("t", target="beta", cost="suite")], rec, allow_suite=True, suite_grant="grant")
    assert rows[0]["reason"] == "no-grant" and rec.started == []
    rec = Recorder(codes={"grant": 1})
    _, rows = _run_wide(tmp_path / "y", steps, rec, allow_suite=True, suite_grant="grant")
    assert rows[1]["reason"] == "no-grant" and rec.started == ["grant"]


def test_suite_step_runs_with_allow_and_grant(tmp_path):
    rec = Recorder()
    steps = [_wide("grant"), _wide("t", target="beta", cost="suite")]
    _, rows = _run_wide(tmp_path, steps, rec, allow_suite=True, suite_grant="grant")
    assert rec.started == ["grant", "t"] and rows[1]["status"] == "ran"


def test_launch_row_carries_child_run_id(tmp_path):
    rid = "20261012T101010Z-1234560abc"
    rec = Recorder(stdout={"l": f"digest: /x/{rid}/digest.json\nWorkflow(...)\n"})
    _, rows = _run_wide(tmp_path, [_wide("l", cost="launch")], rec)
    assert rows[0]["status"] == "ran" and rows[0]["reason"] == f"child-run:{rid}"


def test_launch_already_ran_and_in_flight(tmp_path):
    rec = Recorder(stdout={"l": '{"state": "already-ran", "run_id": "20261012T101010Z-already-ran"}'})
    _, rows = _run_wide(tmp_path, [_wide("l", cost="launch")], rec)
    assert rows[0]["status"] == "ran" and rows[0]["reason"] == "already-ran"
    rec = Recorder(codes={"l": 3})
    _, rows = _run_wide(tmp_path / "y", [_wide("l", cost="launch")], rec)
    assert rows[0]["status"] == "failed"


def test_cold_stdin_reaches_the_child(tmp_path):
    import sys

    script = tmp_path / "echo.py"
    script.write_text("import sys\nprint(sys.stdin.read().upper(), sys.argv[1:])\n", encoding="utf-8")
    out = runner._measure_cold(
        [sys.executable, str(script), "x"], repo_root=tmp_path, deadline_s=30.0, stdin_text="hello"
    )
    assert out.exit_code == 0 and out.stdout.strip() == "HELLO ['x']"


def test_source_names_no_ceremony():
    for mod in (runner, ledger):
        text = open(mod.__file__, encoding="utf-8").read()
        assert "workday-start" not in text and "workday_start" not in text


def _op_step():
    return StepSpec(
        id="o", stage="s", kind="op", target="git.maintenance", args=('{"k": 1}',), depends_on=None,
        already_satisfied=False, detail="", heading="o", cost_class="engine",
    )


def test_op_dispatch_warm_path_carries_origin_worktree(tmp_path, monkeypatch):
    seen = {}

    def fake_try(msg, read_deadline_secs):
        seen.update(msg)
        return {"result": {"ok": True}}

    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", fake_try)
    out = runner._dispatch_op(_op_step(), repo_root=tmp_path, run_dir=tmp_path, deadline_s=5.0)
    assert out.exit_code == 0
    assert seen["_origin_worktree"] == str(tmp_path) and seen["params"] == {"k": 1}


def test_op_dispatch_cold_path_carries_origin_worktree(tmp_path, monkeypatch):
    from coordinator_core import ipc

    seen = {}

    async def fake_dispatch(msg):
        seen.update(msg)
        return {"result": {"ok": True}}

    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(ipc, "dispatch_message", fake_dispatch)
    out = runner._dispatch_op(_op_step(), repo_root=tmp_path, run_dir=tmp_path, deadline_s=5.0)
    assert out.exit_code == 0
    assert seen["_origin_worktree"] == str(tmp_path)


def test_failed_reason_prefers_the_drift_line_over_a_leading_ok_line():
    outcome = ChildOutcome(
        exit_code=1,
        stdout="[ok] coordinator-claude: n/a by design\n[drift] example-game-repo: installed copy lags\n",
    )
    result = runner._result_from(_step("a"), outcome)
    assert result.reason == "exit 1: [drift] example-game-repo: installed copy lags"


def test_failed_reason_without_a_marker_line_keeps_the_first_line():
    result = runner._result_from(_step("a"), ChildOutcome(exit_code=1, stdout="first\nsecond\n"))
    assert result.reason == "exit 1: first"


def _sstep(id_, *, gates=False, kind="cli", stage="s", stdin_from=None):
    return dataclasses.replace(
        _wide(id_, gates=gates, stdin_from=stdin_from), kind=kind, stage=stage
    )


class Overlap:
    """A cli table whose steps wait on a two-party barrier: only overlapping steps pass."""

    def __init__(self, waiters):
        self.barrier = threading.Barrier(2)
        self.waiters = set(waiters)
        self.threads: dict[str, str] = {}

    def table(self):
        def run(step, *, repo_root, run_dir, deadline_s, stdin_text=None):
            self.threads[step.id] = threading.current_thread().name
            if step.id in self.waiters:
                try:
                    self.barrier.wait(timeout=5)
                except threading.BrokenBarrierError:
                    return ChildOutcome(exit_code=9, process_ms=1.0, spawns=1, stderr="serial")
            return ChildOutcome(exit_code=0, process_ms=1.0, spawns=1)

        return {"cli": run, "op": run, "inproc": run, "rule-child": lambda *a, **k: ChildOutcome(0)}


def test_a_concurrent_stage_overlaps_its_steps_and_keeps_step_order(tmp_path):
    steps = [_sstep("a"), _sstep("b"), _sstep("c")]
    ov = Overlap({"a", "c"})
    run_dir = tmp_path / "run"
    results = run_stages(
        steps, ceremony="demo", repo_root=tmp_path, run_dir=run_dir, dispatch=ov.table(),
        concurrent_stages=frozenset({"s"}),
    )
    assert [r.status for r in results] == ["ran"] * 3
    assert [r["id"] for r in ledger.read(run_dir)] == ["a", "b", "c"]
    assert all(isinstance(r["wall_ms"], float) for r in ledger.read(run_dir))


def test_without_the_seam_the_same_steps_run_serially(tmp_path):
    ov = Overlap({"a", "c"})
    ov.barrier = threading.Barrier(2, timeout=0.2)
    results = run_stages(
        [_sstep("a"), _sstep("b"), _sstep("c")], ceremony="demo", repo_root=tmp_path, run_dir=tmp_path / "run",
        dispatch=ov.table(),
    )
    assert results[0].status == "failed" and "serial" in results[0].reason + results[0].stderr


def test_ineligible_steps_stay_serial_on_the_calling_thread(tmp_path):
    ov = Overlap(set())
    steps = [
        _sstep("a"), _sstep("g", gates=True), _sstep("i", kind="inproc"),
        _sstep("o", kind="op"), _sstep("s", stdin_from="a"), _sstep("other", stage="t"),
    ]
    results = run_stages(
        steps, ceremony="demo", repo_root=tmp_path, run_dir=tmp_path / "run", dispatch=ov.table(),
        concurrent_stages=frozenset({"s"}),
    )
    assert [r.id for r in results] == ["a", "g", "i", "o", "s", "other"]
    assert set(ov.threads.values()) == {threading.current_thread().name}


def test_a_group_charges_the_budget_per_step_and_refuses_past_the_cap(tmp_path):
    steps = [_sstep(x) for x in "abcd"]
    rec = Recorder()
    budget = CompositionBudget(composition_id="t", max_invocations=2)
    results = run_stages(
        steps, ceremony="demo", repo_root=tmp_path, run_dir=tmp_path / "run", dispatch=rec.table(),
        budget=budget, concurrent_stages=frozenset({"s"}),
    )
    statuses = [r.status for r in results]
    assert statuses.count("ran") == 2 and statuses.count("not-composed") == 2
    assert [r.status for r in results[:2]] == ["ran", "ran"]


def test_a_raising_member_is_a_failed_row_and_the_rest_still_run(tmp_path):
    fake = Fake(raises={"b"})
    run_dir = tmp_path / "run"
    results = run_stages(
        [_sstep("a"), _sstep("b"), _sstep("c")], ceremony="demo", repo_root=tmp_path, run_dir=run_dir,
        dispatch=fake.table(), concurrent_stages=frozenset({"s"}),
    )
    assert [r.status for r in results] == ["ran", "failed", "ran"]
    assert "boom" in results[1].reason
