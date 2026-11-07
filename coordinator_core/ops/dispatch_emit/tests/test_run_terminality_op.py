"""Tests for ``dispatch.run_terminality``: every classification row, path refusals, zero spawns."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
from coordinator_core.ops.dispatch_emit import run_terminality as op

RUN = "warp-demo-20261101T000000-abc123"


def _cpu() -> float:
    """Process time including reaped children; `time.process_time` omits them."""
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


@pytest.fixture
def record(tmp_path):
    rec = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    return rec


def _halt(record, **extra):
    halted_run._write(record, {"status": halted_run.STATUS_HALTED, "halted": RUN, "sizing_rel": None, **extra})


def _call(root, run_dir=None):
    return op._handler({"run_dir": run_dir or f"{ask_contract.RECORD_DIR_ROOT}/{RUN}"}, root)


def _expect(reply, terminal, resumable, reason):
    assert reply == {"ok": True, "run_id": RUN, "terminal": terminal, "resumable": resumable, "reason": reason}


def test_live_halt_is_resumable(tmp_path, record):
    _halt(record)
    _expect(_call(tmp_path), False, True, "live halt")


def test_halt_then_mark_landed_is_terminal(tmp_path, record):
    _halt(record)
    assert halted_run.mark_landed(record, "a" * 40)
    _expect(_call(tmp_path), True, False, "halt marker landed")


@pytest.mark.parametrize("status", ["superseded", "closed"])
def test_superseded_and_closed_are_terminal(tmp_path, record, status):
    halted_run._write(record, {"status": status, "halted": RUN})
    _expect(_call(tmp_path), True, False, f"halt marker {status}")


def test_released_halt_stays_resumable(tmp_path, record):
    _halt(record)
    assert halted_run.record_released(record, "b" * 40, "shipped elsewhere")
    _expect(_call(tmp_path), False, True, "halt released; still resumable")


def test_resumed_is_neither(tmp_path, record):
    _halt(record)
    assert halted_run.mark_resumed(record)
    _expect(_call(tmp_path), False, False, "resumed by a successor run")


def test_unknown_status_keeps_the_dir(tmp_path, record):
    halted_run._write(record, {"status": "weird"})
    _expect(_call(tmp_path), False, False, "unrecognised halt marker status")


@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_corrupt_marker_is_neither(tmp_path, record, body):
    (record / halted_run.HALTED_FILE).write_text(body, encoding="utf-8")
    _expect(_call(tmp_path), False, False, "unreadable halt marker")


def _outcome(record, outcome):
    (record / ask_contract.RUN_OUTCOME_FILE).write_text(
        json.dumps({"inline_review": {}, "objective_loop": {"outcome": outcome}}), encoding="utf-8"
    )


@pytest.mark.parametrize("outcome", ["impossible", "doctrine_conflict"])
def test_terminal_run_outcome_without_marker_is_terminal(tmp_path, record, outcome):
    _outcome(record, outcome)
    _expect(_call(tmp_path), True, False, f"run outcome {outcome}")


def test_non_terminal_run_outcome_fails_closed(tmp_path, record):
    _outcome(record, "exhausted")
    _expect(_call(tmp_path), False, False, "no halt marker or terminal outcome")


def test_marker_wins_over_run_outcome(tmp_path, record):
    _outcome(record, "impossible")
    _halt(record)
    _expect(_call(tmp_path), False, True, "live halt")


def test_empty_dir_is_neither(tmp_path, record):
    _expect(_call(tmp_path), False, False, "no halt marker or terminal outcome")


def test_backslash_path_resolves(tmp_path, record):
    _halt(record)
    _expect(_call(tmp_path, f"{ask_contract.RECORD_DIR_ROOT}\\{RUN}".replace("/", "\\")), False, True, "live halt")


@pytest.mark.parametrize("bad", [
    "state/warp/../../etc", "state", "state/warp", "state/warp/a/b", "../outside", "other/dir",
    "state/warp/missing-run", "", "/abs/state/warp/x",
])
def test_paths_outside_record_root_or_missing_refuse_and_write_nothing(tmp_path, record, bad):
    _halt(record)
    (tmp_path / "other" / "dir").mkdir(parents=True)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    reply = _call(tmp_path, bad) if bad else op._handler({"run_dir": bad}, tmp_path)
    assert reply["ok"] is False and reply["error"]
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_file_instead_of_dir_and_missing_param_refuse(tmp_path, record):
    (record.parent / "afile").write_text("x", encoding="utf-8")
    assert _call(tmp_path, f"{ask_contract.RECORD_DIR_ROOT}/afile")["ok"] is False
    assert op._handler({}, tmp_path)["ok"] is False
    assert op._handler({"run_dir": RUN}, None)["ok"] is False


def test_zero_spawns_and_under_50ms(tmp_path, record, monkeypatch):
    _halt(record)
    spawned = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, args, *a, **kw):
            spawned.append(list(args)[:1])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    start = _cpu()
    reply = _call(tmp_path)
    elapsed = _cpu() - start
    monkeypatch.undo()
    assert reply["ok"] is True and reply["resumable"] is True
    assert spawned == []
    assert elapsed < 0.05
