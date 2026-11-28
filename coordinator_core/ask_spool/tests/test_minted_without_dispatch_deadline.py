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
from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    stamp_plan_phase_at,
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


DIED = "ask-20261122T123535-7164a9"
STAGED_LATE = wrd.STAGED_STALE_SECONDS + 60


def _died(root, run_id=DIED, *, session="sess-died", stage="minted", plan_started=True):
    """A run whose plan stage began (`plan_started_at`, no `phase`) and then stopped, at `stage`."""
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(rec, "minted", session_id=session)
    if plan_started:
        assert stamp_plan_phase_at(rec)
    if stage == "staged":
        write_run_stage_at(rec, "staged")
    return rec


def _died_halts(root, session="sess-died"):
    return [n for n in _notices(root, session) if n.get("status") == "halted"]


def _active_child_of(root, parent, now):
    recent = now - 30
    child_dir = root / RECORD_DIR_ROOT / "warp-child-20261122T140000-abcdef"
    child_dir.mkdir(parents=True)
    (child_dir / "run.json").write_text(json.dumps({"chain_parent": parent}), encoding="utf-8")
    stamp = datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_stage_at(child_dir, "staged", now=stamp)
    for path in (*child_dir.iterdir(), child_dir):
        os.utime(path, (recent, recent))


def test_a_minted_run_with_plan_started_and_no_phase_is_halted_died_mid_stage_and_noticed(tmp_path):
    _died(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    marker = _marker(tmp_path, DIED)
    assert marker["status"] == "halted" and marker["halted"] == drain.RUN_DIED_MID_STAGE == "died_mid_stage"
    assert "cause unknown" in marker["reason"] and "run-stage at minted" in marker["reason"]
    (halt,) = _died_halts(tmp_path)
    assert halt["run_id"] == DIED and halt["halted"] == "died_mid_stage" and halt["reason"] == marker["reason"]


def test_a_staged_run_with_plan_started_and_no_phase_is_halted_past_the_staged_bound(tmp_path):
    _died(tmp_path, stage="staged")
    drain.run(str(tmp_path), now=time.time() + STAGED_LATE)
    marker = _marker(tmp_path, DIED)
    assert marker["halted"] == "died_mid_stage" and "run-stage at staged" in marker["reason"]
    (halt,) = _died_halts(tmp_path)
    assert halt["reason"] == marker["reason"]


def test_a_staged_run_inside_the_staged_bound_but_past_the_minted_deadline_is_untouched(tmp_path):
    _died(tmp_path, stage="staged")
    assert LATE < wrd.STAGED_STALE_SECONDS
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, DIED) is None
    assert _died_halts(tmp_path) == []


@pytest.mark.parametrize("stage", ["minted", "staged"])
def test_a_fresh_run_that_started_its_plan_is_untouched(tmp_path, stage):
    _died(tmp_path, stage=stage)
    drain.run(str(tmp_path), now=time.time() + 10)
    assert _marker(tmp_path, DIED) is None
    assert _died_halts(tmp_path) == []


def test_a_staged_run_without_plan_started_is_untouched_however_late(tmp_path):
    _died(tmp_path, stage="staged", plan_started=False)
    drain.run(str(tmp_path), now=time.time() + STAGED_LATE)
    assert _marker(tmp_path, DIED) is None


def test_a_run_with_a_live_run_token_is_untouched(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_token

    (tmp_path / ".git").mkdir()
    _died(tmp_path)
    run_token.mint(tmp_path, DIED, f"scratch/warp/{DIED}.workflow.mjs", session_id="sess-died", marker=True)
    assert run_token.is_live(tmp_path, DIED)
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, DIED) is None
    assert _died_halts(tmp_path) == []


def test_a_halted_died_run_is_not_rewritten_and_a_landed_one_is_untouched(tmp_path):
    halted = _died(tmp_path, "ask-20261122T123535-aaaaaa", session="sess-h")
    halted_run.write_halt_marker(halted, halted="other", sizing_rel=None, reason="prior")
    landed = _died(tmp_path, "ask-20261122T123535-bbbbbb", session="sess-l")
    halted_run.write_halt_marker(landed, halted="other", sizing_rel=None, reason="prior")
    assert halted_run.mark_landed(landed, "0" * 40)
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, "ask-20261122T123535-aaaaaa")["halted"] == "other"
    assert _marker(tmp_path, "ask-20261122T123535-bbbbbb")["status"] == "landed"
    assert _died_halts(tmp_path, "sess-h") == [] and _died_halts(tmp_path, "sess-l") == []


def test_a_died_run_with_an_active_chained_child_is_untouched(tmp_path):
    _died(tmp_path)
    now = time.time() + LATE
    _active_child_of(tmp_path, DIED, now)
    drain.run(str(tmp_path), now=now)
    assert _marker(tmp_path, DIED) is None
    assert _died_halts(tmp_path) == []


def test_a_second_drain_does_not_rewrite_the_died_mid_stage_marker(tmp_path):
    _died(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    first = _marker(tmp_path, DIED)
    drain.run(str(tmp_path), now=time.time() + LATE + 60)
    assert _marker(tmp_path, DIED) == first
    assert len(_died_halts(tmp_path)) == 1


def test_the_minted_without_dispatch_reasons_are_unchanged_beside_a_died_run(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    _child(tmp_path)
    _died(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, run_id)["halted"] == "minted_without_dispatch"
    assert "never dispatched: minted" in _marker(tmp_path, run_id)["reason"]
    assert _marker(tmp_path, CHILD)["halted"] == "minted_without_dispatch"
    assert "stalled at minted/executing" in _marker(tmp_path, CHILD)["reason"]
    assert _marker(tmp_path, DIED)["halted"] == "died_mid_stage"


def test_an_already_halted_run_is_not_rewritten(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    halted_run.write_halt_marker(tmp_path / RECORD_DIR_ROOT / run_id, halted="other", sizing_rel=None, reason="prior")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, run_id)["halted"] == "other"
    assert _halts(tmp_path) == []
