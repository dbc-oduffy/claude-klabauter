"""An XS-MINT-MISROUTED halt records a stage_by_plan_path next_action; every other halt keeps the revise one."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose


def _halt_next_action(monkeypatch, *, in_run_commit: bool) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", in_run_commit=in_run_commit)
    line = script[script.index("  _nextAction = _closeable") :].split("\n", 1)[0]
    return line.split("_halted.next_action : ", 1)[1]


def test_misrouted_halt_names_the_plan_path_stage_and_other_halts_keep_revise(monkeypatch):
    tail = _halt_next_action(monkeypatch, in_run_commit=True)
    cond, rest = tail.split(" ? ", 1)
    assert "XS-MINT-MISROUTED" in cond
    misrouted, other = rest.split(" : ", 1)
    assert misrouted.startswith("{ kind: 'stage_by_plan_path'")
    assert "revise_and_refire" not in misrouted
    assert other.startswith(ask_compose.REVISE_NEXT_ACTION_JS)


def test_a_run_without_in_run_commit_still_records_none(monkeypatch):
    tail = _halt_next_action(monkeypatch, in_run_commit=False)
    assert tail.startswith("{ kind: 'none'") and "XS-MINT-MISROUTED" not in tail
