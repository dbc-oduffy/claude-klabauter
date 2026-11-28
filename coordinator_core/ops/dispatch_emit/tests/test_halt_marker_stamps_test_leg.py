"""Every halted `halted.json` carries the test leg's measured-at time and HEAD, so a stale citation is visible."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.git import git_state
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, write_halt_marker

RUN = "warp-2026-11-24-x-20261124T000000-aaaaaa"
HEAD = "a" * 40


def _record_dir(tmp_path: Path) -> Path:
    rec = tmp_path / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    return rec


def _leg(rec: Path, **fields) -> None:
    (rec / "test-leg.json").write_text(json.dumps({"status": "fail", **fields}), encoding="utf-8")


def _halted(rec: Path) -> dict:
    return json.loads((rec / HALTED_FILE).read_text(encoding="utf-8"))


def test_a_halt_with_no_leg_stamps_null_values_and_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    rec = _record_dir(tmp_path)
    write_halt_marker(rec, halted="criterion_not_met", sizing_rel=None)
    assert _halted(rec)["test_leg"] == {"measured_at": None, "head_sha": None, "current_head": HEAD, "stale": True}


def test_a_halt_stamps_the_legs_measured_at_and_head(tmp_path, monkeypatch):
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    rec = _record_dir(tmp_path)
    _leg(rec, measured_at="2026-11-24T20:00:00+00:00", head_sha=HEAD)
    write_halt_marker(rec, halted="criterion_not_met", sizing_rel=None)
    assert _halted(rec)["test_leg"] == {
        "measured_at": "2026-11-24T20:00:00+00:00", "head_sha": HEAD, "current_head": HEAD, "stale": False}


def test_a_leg_measured_on_an_older_head_reads_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    rec = _record_dir(tmp_path)
    _leg(rec, measured_at="2026-11-24T20:00:00+00:00", head_sha="b" * 40)
    write_halt_marker(rec, halted="criterion_not_met", sizing_rel=None)
    assert _halted(rec)["test_leg"]["stale"] is True


def test_a_re_halt_refreshes_the_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    rec = _record_dir(tmp_path)
    _leg(rec, measured_at="2026-11-24T19:00:00+00:00", head_sha="b" * 40)
    write_halt_marker(rec, halted="criterion_not_met", sizing_rel=None)
    _leg(rec, measured_at="2026-11-24T21:00:00+00:00", head_sha=HEAD)
    write_halt_marker(rec, halted="criterion_not_met", sizing_rel=None)
    stamp = _halted(rec)["test_leg"]
    assert (stamp["measured_at"], stamp["head_sha"], stamp["stale"]) == ("2026-11-24T21:00:00+00:00", HEAD, False)


def test_a_non_halted_payload_is_written_without_a_stamp(tmp_path):
    rec = _record_dir(tmp_path)
    _leg(rec, measured_at="2026-11-24T20:00:00+00:00", head_sha=HEAD)
    halted_run._write(rec, {"status": "resumed", "sizing_rel": None})
    assert "test_leg" not in _halted(rec)
