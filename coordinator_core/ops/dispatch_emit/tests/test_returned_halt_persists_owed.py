"""land_returned_halt persists manifest owed[] as state/owed-legs rows."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.returned_halt import land_returned_halt

_OWED = [
    {"id": "C7", "reason": "em_performed", "detail": "wire it"},
    {"id": "C8", "reason": "operator", "detail": ""},
]


def _record_dir(tmp_path: Path, owed=_OWED) -> Path:
    rd = tmp_path / "state" / "warp" / "run-1"
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    if owed is not None:
        (rd / "manifest.json").write_text(json.dumps({"owed": owed}), encoding="utf-8")
    return rd


def _result(**extra) -> dict:
    return {"halted_by": {"halted": "refusal", "plan": "docs/plans/p.md", **extra}, "halt_commit": None}


def _leg(tmp_path: Path, row_id: str) -> dict:
    return yaml.safe_load((tmp_path / "state" / "owed-legs" / f"run-1--{row_id}.yaml").read_text(encoding="utf-8"))


def test_owed_rows_persist_when_the_halt_lands(tmp_path):
    rd = _record_dir(tmp_path)
    assert land_returned_halt(rd, _result(), launched_at=time.time() - 60) == rd / "halted.json"
    leg = _leg(tmp_path, "C7")
    assert (leg["run_id"], leg["plan"], leg["reason"]) == ("run-1", "docs/plans/p.md", "em_performed")
    assert _leg(tmp_path, "C8")["reason"] == "operator"


def test_owed_rows_persist_when_the_script_already_committed_the_halt(tmp_path):
    rd = _record_dir(tmp_path)
    result = {**_result(), "halt_commit": {"committed": True}}
    assert land_returned_halt(rd, result, launched_at=time.time() - 60) is None
    assert _leg(tmp_path, "C7")["id"] == "C7"


def test_absent_manifest_writes_no_owed_legs_and_still_lands(tmp_path):
    rd = _record_dir(tmp_path, owed=None)
    assert land_returned_halt(rd, _result(), launched_at=time.time() - 60) == rd / "halted.json"
    assert not (tmp_path / "state" / "owed-legs").exists()


def test_empty_owed_writes_nothing(tmp_path):
    rd = _record_dir(tmp_path, owed=[])
    land_returned_halt(rd, _result(), launched_at=time.time() - 60)
    assert not (tmp_path / "state" / "owed-legs").exists()


def test_a_result_without_halted_by_persists_nothing(tmp_path):
    rd = _record_dir(tmp_path)
    assert land_returned_halt(rd, {"halt_commit": None}, launched_at=time.time() - 60) is None
    assert not (tmp_path / "state" / "owed-legs").exists()
