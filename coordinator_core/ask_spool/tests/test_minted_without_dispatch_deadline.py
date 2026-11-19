"""A spool entry whose run stays at `minted` past the dispatch deadline is halted and its submitter noticed."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from coordinator_core.ask_spool import drain, record
from coordinator_core.ask_spool.tests.test_drain import Engine, _notices, _spool
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    stamp_run_phase_at,
    write_run_stage,
    write_run_stage_at,
)

LATE = drain.MINTED_DISPATCH_DEADLINE_SECONDS + 60


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _minted_entry(root, stage="minted"):
    sid = _spool(root)
    drain.run(str(root), evaluate=Engine(root))
    header, _ = record.read_entry(record.entry_path(str(root), sid, record.MINTED_SUFFIX))
    run_id = header["run_id"]
    write_run_stage(root, run_id, stage)
    return sid, run_id


def _marker(root, run_id):
    path = root / RECORD_DIR_ROOT / run_id / "halted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _halts(root):
    return [n for n in _notices(root) if n.get("status") == "halted"]


def test_minted_past_the_deadline_is_halted_with_reason_and_noticed(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    marker = _marker(tmp_path, run_id)
    assert marker["status"] == "halted" and marker["halted"] == "minted_without_dispatch"
    assert "never dispatched" in marker["reason"] and "workflow" in marker["reason"]
    (halt,) = _halts(tmp_path)
    assert halt["run_id"] == run_id and halt["halted"] == "minted_without_dispatch" and halt["reason"] == marker["reason"]


def test_a_fresh_entry_is_untouched(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    drain.run(str(tmp_path), now=time.time() + 10)
    assert _marker(tmp_path, run_id) is None
    assert _halts(tmp_path) == []


def test_a_run_past_minted_is_untouched(tmp_path):
    _, run_id = _minted_entry(tmp_path, stage="staged")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, run_id) is None
    assert _halts(tmp_path) == []


def test_a_second_drain_does_not_halt_again(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    first = _marker(tmp_path, run_id)
    drain.run(str(tmp_path), now=time.time() + LATE + 60)
    assert _marker(tmp_path, run_id) == first
    assert len(_halts(tmp_path)) == 1


def test_a_launcher_parent_with_an_active_chained_child_is_untouched(tmp_path):
    _, parent = _minted_entry(tmp_path)
    now = time.time() + LATE
    recent = now - 30
    child = "warp-child-20261115T203651-abcdef"
    child_dir = tmp_path / RECORD_DIR_ROOT / child
    child_dir.mkdir(parents=True)
    (child_dir / "run.json").write_text(json.dumps({"chain_parent": parent}), encoding="utf-8")
    stamp = datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_stage_at(child_dir, "staged", now=stamp)
    for path in (*child_dir.iterdir(), child_dir):
        os.utime(path, (recent, recent))
    drain.run(str(tmp_path), now=now)
    assert _marker(tmp_path, parent) is None
    assert _halts(tmp_path) == []


def test_a_silent_chained_child_does_not_save_the_parent(tmp_path):
    _, parent = _minted_entry(tmp_path)
    child = "warp-child-20261115T203651-abcdef"
    child_dir = tmp_path / RECORD_DIR_ROOT / child
    child_dir.mkdir(parents=True)
    (child_dir / "run.json").write_text(json.dumps({"chain_parent": parent}), encoding="utf-8")
    write_run_stage(tmp_path, child, "staged")
    drain.run(str(tmp_path), now=time.time() + 4 * 3600)
    assert _marker(tmp_path, parent)["halted"] == "minted_without_dispatch"


CHILD = "ask-20261118T092530-907bf8"


def _child(root, run_id=CHILD, *, session="sess-child", phase="executing", stage="minted"):
    """A launcher child: a record dir with a run-stage and no spool `.minted` entry."""
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(rec, stage, session_id=session)
    if phase:
        assert stamp_run_phase_at(rec, phase)
    return rec


def _child_halts(root, session="sess-child"):
    return [n for n in _notices(root, session) if n.get("status") == "halted"]


def test_a_launcher_child_at_minted_executing_with_no_run_dir_is_halted_naming_the_stall(tmp_path):
    _child(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    marker = _marker(tmp_path, CHILD)
    assert marker["status"] == "halted" and marker["halted"] == "minted_without_dispatch"
    assert "stalled at minted/executing with no run dir" in marker["reason"]
    assert "never dispatched" not in marker["reason"]
    (halt,) = _child_halts(tmp_path)
    assert halt["run_id"] == CHILD and halt["reason"] == marker["reason"]


def test_a_launcher_child_without_a_stamped_session_is_halted_with_no_notice(tmp_path):
    _child(tmp_path, session=None)
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, CHILD)["halted"] == "minted_without_dispatch"
    assert _child_halts(tmp_path) == [] and _halts(tmp_path) == []


def test_a_launcher_child_inside_the_deadline_is_untouched(tmp_path):
    _child(tmp_path)
    drain.run(str(tmp_path), now=time.time() + 10)
    assert _marker(tmp_path, CHILD) is None
    assert _child_halts(tmp_path) == []


def test_a_launcher_child_past_the_lookback_window_is_untouched(tmp_path):
    _child(tmp_path)
    drain.run(str(tmp_path), now=time.time() + drain.MINTED_SWEEP_LOOKBACK_SECONDS + 3600)
    assert _marker(tmp_path, CHILD) is None


def test_a_launcher_child_with_a_run_dir_is_untouched(tmp_path):
    _child(tmp_path)
    run_dir = tmp_path / "scratch" / "warp" / CHILD
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, CHILD) is None


def test_a_run_dir_holding_only_submission_json_counts_as_no_run_dir(tmp_path):
    _child(tmp_path)
    run_dir = tmp_path / "scratch" / "warp" / CHILD
    run_dir.mkdir(parents=True)
    (run_dir / "submission.json").write_text("{}", encoding="utf-8")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, CHILD)["halted"] == "minted_without_dispatch"


@pytest.mark.parametrize("phase,stage", [(None, "minted"), ("review", "minted"), ("executing", "staged")])
def test_a_launcher_child_not_at_minted_executing_is_untouched(tmp_path, phase, stage):
    _child(tmp_path, phase=phase, stage=stage)
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, CHILD) is None


def test_a_launcher_child_with_an_active_chained_child_is_untouched(tmp_path):
    _child(tmp_path)
    now = time.time() + LATE
    recent = now - 30
    grandchild = "warp-child-20261118T092600-abcdef"
    gdir = tmp_path / RECORD_DIR_ROOT / grandchild
    gdir.mkdir(parents=True)
    (gdir / "run.json").write_text(json.dumps({"chain_parent": CHILD}), encoding="utf-8")
    stamp = datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_stage_at(gdir, "staged", now=stamp)
    for path in (*gdir.iterdir(), gdir):
        os.utime(path, (recent, recent))
    drain.run(str(tmp_path), now=now)
    assert _marker(tmp_path, CHILD) is None


def test_a_launcher_child_halt_is_not_rewritten_by_a_second_drain(tmp_path):
    _child(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    first = _marker(tmp_path, CHILD)
    drain.run(str(tmp_path), now=time.time() + LATE + 60)
    assert _marker(tmp_path, CHILD) == first
    assert len(_child_halts(tmp_path)) == 1


def test_a_halted_launcher_child_is_not_rewritten(tmp_path):
    rec = _child(tmp_path)
    halted_run.write_halt_marker(rec, halted="other", sizing_rel=None, reason="prior")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, CHILD)["halted"] == "other"
    assert _child_halts(tmp_path) == []


def test_the_spool_halt_reason_is_unchanged_beside_the_child_sweep(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    _child(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert "never dispatched: minted" in _marker(tmp_path, run_id)["reason"]
    assert "stalled at minted/executing" in _marker(tmp_path, CHILD)["reason"]


def test_the_child_sweep_over_2000_record_dirs_is_cheap_and_spawns_nothing(tmp_path, monkeypatch):
    base = tmp_path / RECORD_DIR_ROOT
    base.mkdir(parents=True)
    long_ago = time.time() - 2 * drain.MINTED_SWEEP_LOOKBACK_SECONDS
    for n in range(1995):
        path = base / f"ask-20261118T000000-{n:06x}"
        path.mkdir()
        if n >= 95:
            os.utime(path, (long_ago, long_ago))
    for n in range(5):
        _child(tmp_path, f"ask-20261118T010000-{n:06x}")
    spawned: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("spawned"))
    now = time.time() + LATE
    started = time.process_time()
    drain._sweep_stalled_launcher_children(str(tmp_path), now)
    elapsed = time.process_time() - started
    assert spawned == []
    assert elapsed < 0.2, f"{elapsed * 1000:.0f}ms"
    assert all(_marker(tmp_path, f"ask-20261118T010000-{n:06x}") for n in range(5))
    print(f"launcher-child sweep process time {elapsed * 1000:.0f}ms")


def test_an_already_halted_run_is_not_rewritten(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    halted_run.write_halt_marker(tmp_path / RECORD_DIR_ROOT / run_id, halted="other", sizing_rel=None, reason="prior")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, run_id)["halted"] == "other"
    assert _halts(tmp_path) == []
