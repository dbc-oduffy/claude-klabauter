"""resume_ledger.since_halt_shas: plan checkpoints newer than the halt the resume measures from."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_SINCE_HALT_SHAS_FIELD, LedgerHalt
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger
from coordinator_core.ops.dispatch_emit.emit import ResumeLanded
from coordinator_core.ops.dispatch_emit.sizing_fire import _carry, since_halt_shas_of

PLAN = "docs/plans/p.md"
SIZING = "state/sizings/s.yaml"


def _s(c: str) -> str:
    return c * 40


def _halt(**kw) -> LedgerHalt:
    return LedgerHalt("run-1", PLAN, SIZING, ("R1",), halt_sha=_s("h"), **kw)


def _state(ordinals: dict, checkpoints: tuple) -> ResumeLanded:
    return ResumeLanded(
        frozenset(), "run-1", _s("h"), run_halts={"run-1": (frozenset(), _s("h"))},
        run_first_halts={"run-1": _s("h")}, checkpoint_shas=checkpoints, walk_ordinals=ordinals,
    )


def test_field_rides_the_verdict(tmp_path):
    halt = _halt(since_halt_shas=(_s("a"),))
    (tmp_path / ".git").mkdir()
    plan = tmp_path / PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text("---\ntitle: p\n---\n\n# p\n", encoding="utf-8", newline="\n")
    out = _resume_ledger({}, halt, tmp_path)
    assert out[RESUME_LEDGER_SINCE_HALT_SHAS_FIELD] == [_s("a")]


def test_halt_base_keeps_only_newer_checkpoints():
    # walk newest first: c1(0) c2(1) HALT(2) c3(3); the halt is never a checkpoint.
    state = _state({_s("1"): 0, _s("2"): 1, _s("h"): 2, _s("3"): 3}, (_s("1"), _s("2"), _s("3")))
    assert since_halt_shas_of(state, _halt()) == (_s("1"), _s("2"))


def test_since_sha_is_the_base_when_set():
    state = _state({_s("1"): 0, _s("h"): 1, _s("2"): 2, _s("f"): 3}, (_s("1"), _s("2")))
    assert since_halt_shas_of(state, _halt(since_sha=_s("f"))) == (_s("1"), _s("2"))
    assert since_halt_shas_of(state, _halt()) == (_s("1"),)


def test_base_outside_the_walk_is_empty():
    state = _state({_s("1"): 0}, (_s("1"),))
    assert since_halt_shas_of(state, _halt()) == ()
    assert since_halt_shas_of(state, LedgerHalt("r", PLAN, SIZING, ())) == ()


def test_carry_sets_it_on_the_returned_halt():
    state = _state({_s("1"): 0, _s("h"): 1}, (_s("1"),))
    out = _carry(None, PLAN, _halt(halted="commit"), state)
    assert out.since_halt_shas == (_s("1"),)
