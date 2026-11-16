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


def _two_halt_setup(monkeypatch, pointer_plan: str):
    pointer = LedgerHalt("old", pointer_plan, SIZING, ("R1",))
    state = ResumeLanded(
        frozenset(), "new", _sha("b"),
        run_halts={"new": (frozenset(), _sha("b")), "old": (frozenset(), _sha("a"))},
    )
    monkeypatch.setattr(sizing_fire, "_terminal_coded", lambda *a, **k: False)
    monkeypatch.setattr(sizing_fire, "_sha_coded", lambda *a, **k: False)
    monkeypatch.setattr(sizing_fire, "_carry", lambda root, plan, halt, st: halt)
    monkeypatch.setattr(ask_contract, "read_halt_record", lambda root, run, at_sha=None: _rec(run))
    return pointer, state


def test_stale_pointer_run_cites_the_newest_halt_sha(monkeypatch, tmp_path):
    pointer, state = _two_halt_setup(monkeypatch, PLAN)
    monkeypatch.setattr(sizing_fire, "_own_pointer", lambda *a, **k: pointer)
    out = sizing_fire._ledger_halt_for(tmp_path, PLAN, SIZING, [], [], state)
    assert (out.run_id, out.halt_sha) == ("new", _sha("b"))
    assert out.blocking == ("R1",)


def test_newest_halt_for_another_sizing_leaves_the_pointer_run(monkeypatch, tmp_path):
    pointer, state = _two_halt_setup(monkeypatch, PLAN)
    monkeypatch.setattr(sizing_fire, "_own_pointer", lambda *a, **k: pointer)
    monkeypatch.setattr(ask_contract, "read_halt_record", lambda root, run, at_sha=None: _rec(run, "state/sizings/other.yaml"))
    out = sizing_fire._ledger_halt_for(tmp_path, PLAN, SIZING, [], [], state)
    assert (out.run_id, out.halt_sha) == ("old", _sha("a"))


def _record_form_setup(monkeypatch, tmp_path, record_plan: str):
    flat, month = "docs/plans/2026-11-12-x.md", "docs/plans/2026-11/2026-11-12-x.md"
    (tmp_path / flat).parent.mkdir(parents=True)
    (tmp_path / flat).write_text("p\n", encoding="utf-8")
    state = ResumeLanded(frozenset(), "new", _sha("b"), run_halts={"new": (frozenset(), _sha("b"))})
    monkeypatch.setattr(sizing_fire, "_terminal_coded", lambda *a, **k: False)
    monkeypatch.setattr(sizing_fire, "_sha_coded", lambda *a, **k: False)
    monkeypatch.setattr(sizing_fire, "_carry", lambda root, plan, halt, st: halt)
    monkeypatch.setattr(ask_contract, "read_halt_record", lambda root, run, at_sha=None: HaltRecord(run, record_plan, SIZING, frozenset({"C1"}), ("C1",)))
    return flat, month, state


def test_newest_halt_record_naming_the_month_form_resumes_the_flat_plan(monkeypatch, tmp_path):
    flat, month, state = _record_form_setup(monkeypatch, tmp_path, "docs/plans/2026-11/2026-11-12-x.md")
    out = sizing_fire._ledger_halt_for(tmp_path, flat, SIZING, ["C1"], ["C1"], state, pointer=None)
    assert out is not None and (out.run_id, out.halt_sha) == ("new", _sha("b"))


def test_newest_halt_record_naming_another_plan_still_refuses(monkeypatch, tmp_path):
    flat, month, state = _record_form_setup(monkeypatch, tmp_path, "docs/plans/other.md")
    assert sizing_fire._ledger_halt_for(tmp_path, flat, SIZING, ["C1"], ["C1"], state, pointer=None) is None


def test_pointer_recorded_under_the_month_form_is_found_for_the_flat_plan(tmp_path):
    flat, month = "docs/plans/2026-11-12-x.md", "docs/plans/2026-11/2026-11-12-x.md"
    (tmp_path / flat).parent.mkdir(parents=True)
    (tmp_path / flat).write_text("p\n", encoding="utf-8")
    ask_contract.write_ledger_halt(ask_contract.ledger_halt_path(tmp_path, month), LedgerHalt("r", month, SIZING, ("R1",)))
    got = sizing_fire._own_pointer(tmp_path, flat, SIZING)
    assert got is not None and got.plan_rel == flat
    assert sizing_fire._own_pointer(tmp_path, flat, "state/sizings/other.yaml") is None
