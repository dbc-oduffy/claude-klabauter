"""submit_engine_ask._slice_deny: a refire reads the tracked per-run parent copy when the scratch record is gone."""

from __future__ import annotations

import json

from coordinator_core.hooks import submit_engine_ask as sea
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

PARENT = "ask-parent-20261109T105505-f92a64"
OTHER_PARENT = "ask-parent-20261109T105505-000000"
RUN = "ask-20261109T105505-bb81bc"


def _write_copy(root, parent_id):
    rec = c.ParentRecord(parent_id=parent_id, runs=(c.SiblingRun(RUN, "a", status=c.STATUS_RUNNING),))
    path = root / c.run_parent_copy_rel(RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec.to_json()), encoding="utf-8")


def test_absent_scratch_record_reads_tracked_copy(tmp_path):
    _write_copy(tmp_path, PARENT)
    assert not (tmp_path / c.parent_record_rel(PARENT)).exists()
    assert sea._slice_deny(tmp_path, (PARENT, RUN)) is None


def test_tracked_copy_naming_other_parent_still_denies(tmp_path):
    _write_copy(tmp_path, OTHER_PARENT)
    why = sea._slice_deny(tmp_path, (PARENT, RUN))
    assert why is not None and "is not in parent" in why


def test_no_record_anywhere_still_denies(tmp_path):
    why = sea._slice_deny(tmp_path, (PARENT, RUN))
    assert why is not None and "is not in parent" in why


def test_read_parent_without_run_id_ignores_tracked_copy(tmp_path):
    _write_copy(tmp_path, PARENT)
    assert sea._read_parent(tmp_path, PARENT) is None
