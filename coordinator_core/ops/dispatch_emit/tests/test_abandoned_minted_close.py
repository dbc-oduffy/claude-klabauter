"""abandoned_minted_close: dead abandoned_at_minted markers are closed, live ones kept, bounded, spawn-free."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_minted_close as amc
from coordinator_core.ops.dispatch_emit import halted_run, minted_parent, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.ops.warp_run_deliverable import ABANDONED_AT_MINTED, MINTED_STALE_SECONDS

NOW = time.time() + 2 * MINTED_STALE_SECONDS


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _abandoned(repo: Path, run_id: str, **marker_extra) -> Path:
    rdir = record_dir(repo, run_id)
    rdir.mkdir(parents=True)
    halted_run.write_halt_marker(rdir, halted=ABANDONED_AT_MINTED, sizing_rel=None, reason="abandoned")
    if marker_extra:
        marker = halted_run._read(rdir)
        (rdir / "halted.json").write_text(json.dumps({**marker, **marker_extra}), encoding="utf-8")
    return rdir


def _status(rdir: Path) -> str:
    return json.loads((rdir / "halted.json").read_text(encoding="utf-8"))["status"]


def _child(repo: Path, run_id: str, parent: str, age: float) -> Path:
    d = record_dir(repo, run_id)
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"resumed_from": parent}), encoding="utf-8")
    stamp = datetime.fromtimestamp(NOW - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_stage_at(d, "staged", now=stamp)
    when = NOW - age
    os.utime(d / "run.json", (when, when))
    os.utime(d, (when, when))
    return d


def test_dead_shell_marker_is_closed_with_reason_and_stamp(repo):
    rdir = _abandoned(repo, "ask-20261101T000000-aaaaaa")
    assert amc.close_abandoned(repo, NOW) == ["ask-20261101T000000-aaaaaa"]
    marker = json.loads((rdir / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "closed" and marker["closed"]["reason"] and marker["closed"]["at"].endswith("Z")


def test_marker_with_withheld_paths_stays_halted(repo):
    rdir = _abandoned(repo, "ask-20261101T000000-bbbbbb", run_artifacts={"withheld": {"src/x.py": "dirty"}})
    assert amc.close_abandoned(repo, NOW) == []
    assert _status(rdir) == "halted"


def test_other_halt_kinds_are_left_alone(repo):
    rdir = record_dir(repo, "ask-20261101T000000-cccccc")
    rdir.mkdir(parents=True)
    halted_run.write_halt_marker(rdir, halted="commit", sizing_rel=None)
    assert amc.close_abandoned(repo, NOW) == []
    assert _status(rdir) == "halted"


def test_live_token_keeps_the_marker(repo):
    rid = "ask-20261101T000000-dddddd"
    rdir = _abandoned(repo, rid)
    run_token.mint(repo, rid, "ask.mjs", session_id="s")
    assert amc.close_abandoned(repo, NOW) == []
    assert _status(rdir) == "halted"


def test_active_descendant_keeps_the_marker_and_stale_descendant_does_not(repo):
    parent = "ask-20261101T000000-eeeeee"
    rdir = _abandoned(repo, parent)
    child = _child(repo, "warp-child", parent, age=60)
    assert amc.close_abandoned(repo, NOW) == []
    assert _status(rdir) == "halted"
    stale = NOW - MINTED_STALE_SECONDS - 600
    for entry in list(child.iterdir()) + [child]:
        os.utime(entry, (stale, stale))
    write_run_stage_at(child, "staged", now=datetime.fromtimestamp(stale, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    os.utime(child, (stale, stale))
    assert amc.close_abandoned(repo, NOW) == [parent]


def test_bounded_by_limit_and_second_call_finishes(repo):
    ids = [f"ask-20261101T00000{i}-ffffff" for i in range(5)]
    for rid in ids:
        _abandoned(repo, rid)
    assert amc.close_abandoned(repo, NOW, limit=2) == ids[:2]
    assert amc.close_abandoned(repo, NOW, limit=10) == ids[2:]
    assert amc.close_abandoned(repo, NOW) == []


def test_kept_markers_ahead_do_not_consume_the_default_limit(repo):
    kept = [_abandoned(repo, f"ask-20261101T00000{i}-aaaaa1", run_artifacts={"withheld": {"x": "d"}}) for i in range(amc.DEFAULT_LIMIT + 2)]
    ids = [f"ask-20261102T00000{i}-bbbbb2" for i in range(3)]
    dirs = [_abandoned(repo, rid) for rid in ids]
    assert amc.close_abandoned(repo, NOW) == ids
    assert all(_status(d) == "closed" for d in dirs)
    assert all(_status(d) == "halted" for d in kept)


def test_scan_budget_bounds_kept_markers(repo, monkeypatch):
    rdir = _abandoned(repo, "ask-20261101T000000-ccccc3")
    monkeypatch.setattr(amc, "SCAN_BUDGET_SECONDS", -1.0)
    assert amc.close_abandoned(repo, NOW) == []
    assert _status(rdir) == "halted"


def test_skipped_run_ids_stay_halted(repo):
    rdir = _abandoned(repo, "ask-20261101T000000-333333")
    assert amc.close_abandoned(repo, NOW, skip={"ask-20261101T000000-333333"}) == []
    assert _status(rdir) == "halted"


def test_default_limit_is_reap_per_call():
    assert amc.DEFAULT_LIMIT == minted_parent.REAP_PER_CALL


def test_reap_orphans_closes_with_the_remaining_limit_and_spawns_nothing(repo, monkeypatch):
    ids = [f"ask-20261101T00000{i}-111111" for i in range(minted_parent.REAP_PER_CALL + 3)]
    dirs = [_abandoned(repo, rid) for rid in ids]

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    assert minted_parent.reap_orphans(repo, NOW) == []
    closed = [d for d in dirs if _status(d) == "closed"]
    assert len(closed) == minted_parent.REAP_PER_CALL
    assert minted_parent.reap_orphans(repo, NOW) == []
    assert all(_status(d) == "closed" for d in dirs)


def test_main_closes_a_batch_and_prints_ids(repo, capsys):
    rid = "ask-20261101T000000-222222"
    rdir = _abandoned(repo, rid)
    assert amc.main(["--root", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out) == {"closed": [rid]}
    assert _status(rdir) == "closed"
