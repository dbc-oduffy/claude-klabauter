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
