"""Pins the ledger-halt pointer, rulable_ids, and the manifest and verdict fields built on them."""

import json
import subprocess

import pytest

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _halt():
    return c.LedgerHalt("run-1", "docs/plans/p.md", "state/sizings/s.yaml", ("R1", "R2"))


def test_halt_round_trip(tmp_path):
    path = c.ledger_halt_path(tmp_path, "docs/plans/p.md")
    assert path == tmp_path / _DISPATCH_REPORT_DIR / "p" / c.LEDGER_HALT_FILE
    c.write_ledger_halt(path, _halt())
    assert c.read_ledger_halt(path) == _halt()


def test_missing_and_malformed_read_none(tmp_path):
    assert c.read_ledger_halt(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert c.read_ledger_halt(bad) is None
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"run_id": "r"}), encoding="utf-8")
    assert c.read_ledger_halt(missing) is None


@pytest.mark.parametrize("plan", ["/etc/p.md", "../p.md", "a/../../p.md"])
def test_unsafe_plan_rel_reads_none(tmp_path, plan):
    p = tmp_path / "h.json"
    p.write_text(json.dumps(c.LedgerHalt("r", plan, "s.yaml", ()).to_json()), encoding="utf-8")
    assert c.read_ledger_halt(p) is None


def test_unsafe_run_id_reads_none(tmp_path):
    p = tmp_path / "h.json"
    p.write_text(json.dumps(c.LedgerHalt("../r", "p.md", "s.yaml", ()).to_json()), encoding="utf-8")
    assert c.read_ledger_halt(p) is None


def test_rulable_ids():
    rows = [
        {rr.ROW_ID: "R1", rr.ROW_STATUS: rr.STATUS_OPEN},
        {rr.ROW_ID: "R2", rr.ROW_STATUS: rr.STATUS_PARTIAL},
        {rr.ROW_ID: "R3", rr.ROW_STATUS: rr.STATUS_MET},
        {rr.ROW_ID: "R4", rr.ROW_STATUS: rr.STATUS_WAIVED},
        {rr.ROW_ID: "R5", rr.ROW_STATUS: rr.STATUS_DEFERRED},
    ]
    assert c.rulable_ids(rows) == ("R1", "R2", "R5")


def test_manifest_rulable_ids_round_trip():
    m = c.StageManifest("d", (), (), "m", rulable_ids=("R1", "R2"))
    out = json.loads(json.dumps(m.to_json()))
    assert out["rulable_ids"] == ["R1", "R2"]
    assert c.StageManifest.from_json(out) == m
    del out["rulable_ids"]
    assert c.StageManifest.from_json(out).rulable_ids == ()


def test_verdict_resume_ledger_only_when_set():
    assert c.RESUME_LEDGER_KEY not in c.GateVerdict("a", None).to_json()
    v = c.GateVerdict("a", None, resume_ledger=_halt().to_json())
    assert v.to_json()[c.RESUME_LEDGER_KEY] == _halt().to_json()


def test_resume_plan_ledger_defaults_none():
    assert c.ResumePlan("p.md", ("C1",)).ledger is None


def _write_record(tmp_path, halted_extra):
    base = tmp_path / "state" / "warp" / "run-1"
    base.mkdir(parents=True)
    halted = {"halted": "ledger-check", "sizing_rel": "state/sizings/s.yaml", "blocking": ["R1"], **halted_extra}
    (base / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    (base / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1"}]}), encoding="utf-8")


def test_read_halt_record_reads_remaining(tmp_path):
    items = [{"id": "R1", "remaining": ["fix x", 3], "evidence": "falsifier failed"}, {"id": 4}, "junk"]
    _write_record(tmp_path, {c.HALT_BLOCKING_ITEMS_FIELD: items})
    rec = c.read_halt_record(tmp_path, "run-1")
    assert rec.remaining == {"R1": {"remaining": ["fix x"], "evidence": "falsifier failed"}}


def test_read_halt_record_without_key_has_empty_remaining(tmp_path):
    _write_record(tmp_path, {})
    assert dict(c.read_halt_record(tmp_path, "run-1").remaining) == {}


def test_ledger_halt_defaults_keep_four_arg_constructor():
    halt = _halt()
    assert halt.halt_sha is None
    assert dict(halt.remaining) == {}
    assert c.HALT_BLOCKING_ITEMS_FIELD not in halt.to_json()
    sha = "a" * 40
    assert c.LedgerHalt("r", "p.md", "s.yaml", (), sha).halt_sha == sha


def test_resume_field_names_are_distinct():
    names = {
        c.RESUME_LEDGER_HALT_SHA_FIELD,
        c.RESUME_LEDGER_REMAINING_FIELD,
        c.RESUME_LEDGER_NOTES_FIELD,
        c.RESUME_LEDGER_EM_DISCHARGED_FIELD,
        c.RESUME_LEDGER_CARRIED_MET_FIELD,
    }
    assert len(names) == 5
    assert c.TERMINAL_RESUME_LEDGER_PARAM == "resume_ledger"
    assert c.UNCHANGED_SINCE_HALT_REASON


def _rec(run_id, blocking, rulable=("T2", "R2", "R3"), remaining=None):
    return c.HaltRecord(
        run_id, "p.md", "s.yaml", frozenset(), None if blocking is None else tuple(blocking),
        remaining or {}, frozenset(rulable),
    )


_STEP = {"remaining": ["carry path"], "evidence": "e1"}
_OLD = _rec("841059", ["R3"], remaining={"R3": _STEP})
_NEW = _rec(
    "1cc5e9",
    ["T2", "R2", "R3"],
    remaining={
        "T2": {"remaining": ["no change"], "evidence": "x"},
        "R3": {"remaining": ["carry path", "no change since halt"], "evidence": "e2"},
    },
)


def test_read_halt_record_reads_rulable_and_met(tmp_path):
    _write_record(tmp_path, {"blocking": ["R3"]})
    base = tmp_path / "state" / "warp" / "run-1"
    (base / "manifest.json").write_text(json.dumps({"rulable_ids": ["T2", "R2", "R3", 7]}), encoding="utf-8")
    rec = c.read_halt_record(tmp_path, "run-1")
    assert rec.rulable == {"T2", "R2", "R3"}
    assert rec.met == {"T2", "R2"}


def test_met_empty_without_rulable_or_blocking(tmp_path):
    _write_record(tmp_path, {})
    assert c.read_halt_record(tmp_path, "run-1").rulable == frozenset()
    assert _rec("r", None).met == frozenset()
    assert _rec("r", ["R1"], rulable=()).met == frozenset()


def test_carry_forward_incident_chain():
    blocking, remaining, carried = c.carry_forward((_NEW, False), [(_OLD, True)])
    assert blocking == ("R3",)
    assert carried == ("T2", "R2")
    assert remaining["R3"]["remaining"] == ["carry path", "no change since halt"]
    assert remaining["R3"]["evidence"] == "e2"
    assert "T2" not in remaining


def test_carry_forward_landing_newest_carries_nothing():
    blocking, _, carried = c.carry_forward((_NEW, True), [(_OLD, True)])
    assert blocking == ("T2", "R2", "R3")
    assert carried == ()


def test_carry_forward_stops_at_first_landing_halt():
    older = _rec("old", ["T2", "R2", "R3"], rulable=("T2", "R2", "R3"))
    blocking, _, carried = c.carry_forward((_NEW, False), [(_OLD, True), (older, False)])
    assert carried == ("T2", "R2")
    assert blocking == ("R3",)
    mid = _rec("mid", ["T2", "R2", "R3"])
    _, _, carried = c.carry_forward((_NEW, False), [(mid, True), (_OLD, True)])
    assert carried == ()


def test_carry_forward_requires_blocking_in_every_newer_halt():
    mid = _rec("mid", ["R3"])
    _, _, carried = c.carry_forward((_NEW, False), [(mid, False), (_OLD, True)])
    assert carried == ("T2", "R2")
    gap = _rec("gap", ["R3"], rulable=("T2", "R3"))
    older = _rec("older", ["R3"])
    _, _, carried = c.carry_forward((_NEW, False), [(gap, False), (older, True)])
    assert carried == ("T2",)


def test_ledger_halt_carried_met_not_persisted():
    halt = c.LedgerHalt("r", "p.md", "s.yaml", ("R3",), carried_met=("T2",))
    assert "carried_met" not in halt.to_json()
    assert c.LedgerHalt("r", "p.md", "s.yaml", ()).carried_met == ()
