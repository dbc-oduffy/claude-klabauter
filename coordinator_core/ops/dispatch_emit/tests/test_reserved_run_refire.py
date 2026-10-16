"""A halted fan-out slice refires from its own (fully sized) slice sizing; a live one does not."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import parent_record_rel, status_rel

_SIZING = {"em_analysis": {"ask_parent": "p1 r1"}}


def _status(root, status):
    path = root / status_rel("r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": "r1", "status": status}), encoding="utf-8")


def test_a_sized_slice_resolves_its_reserved_run(tmp_path):
    assert op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml") == ("r1", "p1")


def test_a_raw_ask_cannot_take_a_reserved_run(tmp_path):
    with pytest.raises(ValueError, match="not a raw ask"):
        op._reserved_run(tmp_path, "r1", None, None)


def test_a_halted_slice_rebinds_and_a_running_one_is_refused(tmp_path):
    parent = tmp_path / parent_record_rel("p1")
    parent.parent.mkdir(parents=True)
    parent.write_text("{}", encoding="utf-8")
    _status(tmp_path, "halted")
    assert op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml") == ("r1", "p1")
    op._bind_reserved_run(tmp_path, "r1", "p1")
    assert json.loads((tmp_path / status_rel("r1")).read_text())["status"] == "running"
    with pytest.raises(ValueError, match="already bound"):
        op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml")
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(tmp_path, "r1", "p1")
