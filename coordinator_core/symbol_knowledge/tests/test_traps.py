"""Tests for coordinator_core.symbol_knowledge.traps (KR3)."""

from __future__ import annotations

from coordinator_core.symbol_knowledge import contract, sidecar, traps
from coordinator_core.symbol_knowledge.contract import FlaggedSymbol

SYM = FlaggedSymbol("pkg.mod.fn", "pkg/mod.py", "adjust", True)
OTHER = FlaggedSymbol("pkg.other.g", "pkg/other.py", "adjust", True)


def _record(**inputs):
    return {"run_id": "r1", "inputs": inputs}


def test_halt_overlapping_a_flagged_path_adds_exactly_one_line():
    rec = _record(unlanded_paths={"C1": ["pkg/mod.py"], "C2": ["pkg\\mod.py"]})
    out = traps.record_traps(rec, [SYM, OTHER])
    assert list(out) == ["pkg.mod.fn"]
    assert len(out["pkg.mod.fn"]) == 1
    assert out["pkg.mod.fn"][0].startswith("halt in run r1")


def test_revise_and_halt_are_separate_lines():
    rec = _record(
        unlanded_paths={"C1": ["pkg/mod.py"]},
        review_only={"rows": [{"id": "C3", "paths": ["pkg/mod.py"]}]},
    )
    lines = traps.record_traps(rec, [SYM])["pkg.mod.fn"]
    assert [ln.split(" ")[0] for ln in lines] == ["halt", "revise"]


def test_rerun_adds_no_duplicate_against_the_sidecar(tmp_path):
    rec = _record(unlanded_paths={"C1": ["pkg/mod.py"]})
    first = traps.record_traps(rec, [SYM], root=tmp_path)
    assert len(first["pkg.mod.fn"]) == 1
    sidecar.write_sidecar(tmp_path, sidecar.merge_entries({}, {}, first))
    assert traps.record_traps(rec, [SYM], root=tmp_path) == {}


def test_no_overlap_or_no_events_is_empty():
    assert traps.record_traps(_record(unlanded_paths={"C1": ["elsewhere.py"]}), [SYM]) == {}
    assert traps.record_traps({"run_id": "r1"}, [SYM]) == {}
    assert traps.record_traps(_record(unlanded_paths="bad", review_only=3), [SYM]) == {}


def test_line_is_capped_at_200_chars():
    long_path = "pkg/" + "x" * 300 + ".py"
    sym = FlaggedSymbol("pkg.x.f", long_path, "adjust", True)
    out = traps.record_traps(_record(unlanded_paths={"C1": [long_path]}), [sym])
    assert len(out["pkg.x.f"][0]) == traps.MAX_TRAP_CHARS


def test_contract_delegates_and_writes_nothing(tmp_path):
    rec = _record(unlanded_paths={"C1": ["pkg/mod.py"]})
    assert contract.record_traps(rec, [SYM]) == traps.record_traps(rec, [SYM])
    assert not list(tmp_path.iterdir())
