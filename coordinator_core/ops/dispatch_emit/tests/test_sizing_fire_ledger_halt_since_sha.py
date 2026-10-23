"""sizing_fire._carry sets LedgerHalt.since_sha to the first halt of the oldest run it walks."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_contract, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import HaltRecord, LedgerHalt
from coordinator_core.ops.dispatch_emit.emit import ResumeLanded
from coordinator_core.ops.dispatch_emit.sizing_fire import COMMIT_HALT_KIND

PLAN = "docs/plans/p.md"
SIZING = "state/sizings/s.yaml"


def _sha(c: str) -> str:
    return c * 40


def _rec(run: str, sizing: str = SIZING) -> HaltRecord:
    return HaltRecord(run, PLAN, sizing, frozenset({"C1"}), ("C1",))


def _state(run_halts: dict, first: dict | None = None) -> ResumeLanded:
    return ResumeLanded(
        frozenset(), next(iter(run_halts)), run_halts[next(iter(run_halts))][1],
        run_halts=run_halts, run_first_halts={**{r: s for r, (_, s) in run_halts.items()}, **(first or {})},
    )


def _carry(monkeypatch, state, head: str, records: dict, kind: str | None = None):
    monkeypatch.setattr(ask_contract, "read_halt_record", lambda root, run, at_sha=None: records.get(run))
    halt = LedgerHalt(head, PLAN, SIZING, ("C1",), halt_sha=state.run_halts[head][1])
    if kind:
        halt = LedgerHalt(head, PLAN, SIZING, ("C1",), halt_sha=halt.halt_sha, halted=kind)
    return sizing_fire._carry(Path("."), PLAN, halt, state)


def test_two_runs_no_landing_since_is_the_older_halt(monkeypatch, tmp_path):
    state = _state({"new": (frozenset(), _sha("b")), "old": (frozenset(), _sha("a"))})
    out = _carry(monkeypatch, state, "new", {"new": _rec("new"), "old": _rec("old")})
    assert out.since_sha == _sha("a")
    assert out.halt_sha == _sha("b")


def test_one_rebound_run_with_two_halts_since_is_its_first(monkeypatch):
    state = _state({"r": (frozenset(), _sha("c"))}, first={"r": _sha("a")})
    out = _carry(monkeypatch, state, "r", {"r": _rec("r")})
    assert out.since_sha == _sha("a")
    assert out.halt_sha == _sha("c")


def test_walk_stops_at_the_run_that_landed_a_chunk(monkeypatch):
    state = _state({
        "new": (frozenset(), _sha("c")),
        "mid": (frozenset({"C1"}), _sha("b")),
        "old": (frozenset(), _sha("a")),
    })
    records = {r: _rec(r) for r in ("new", "mid", "old")}
    out = _carry(monkeypatch, state, "new", records)
    assert out.since_sha == _sha("b")


def test_earlier_record_for_another_sizing_leaves_the_head_first_halt(monkeypatch):
    state = _state({"new": (frozenset(), _sha("b")), "old": (frozenset(), _sha("a"))}, first={"new": _sha("e")})
    out = _carry(monkeypatch, state, "new", {"new": _rec("new"), "old": _rec("old", "state/sizings/other.yaml")})
    assert out.since_sha == _sha("e")


def test_head_that_landed_a_chunk_does_not_walk_back(monkeypatch):
    state = _state({"new": (frozenset({"C1"}), _sha("b")), "old": (frozenset(), _sha("a"))})
    out = _carry(monkeypatch, state, "new", {"new": _rec("new"), "old": _rec("old")})
    assert out.since_sha == _sha("b")


def test_early_returns_still_set_since(monkeypatch):
    state = _state({"new": (frozenset(), _sha("b")), "old": (frozenset(), _sha("a"))})
    assert _carry(monkeypatch, state, "new", {}).since_sha == _sha("b")
    assert _carry(monkeypatch, state, "new", {"new": _rec("new")}, kind=COMMIT_HALT_KIND).since_sha == _sha("b")
