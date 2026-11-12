"""A halted run's leftover token holds nothing, so it parks no overlapping sibling; a refire still reclaims.

The token a run minted before its `halted` marker is not in `held_records` (the listing every park and
stale-hold consumer reads), while `halt_admits`, `escrowed` and `release` read the record directly and
keep working. A re-mint after the halt (a refire) holds again.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import run_token

pytestmark = pytest.mark.complete

SCRIPT = "scratch/warp/run.workflow.mjs"
SIZING = "state/sizings/s.yaml"


def _rid(age: int) -> str:
    return f"warp-s-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-abcdef"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING).write_text("status: open\n", encoding="utf-8")
    return tmp_path


def _marker(root: Path, run_id: str, status: str, *, mtime: float) -> None:
    rec = root / "state" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    path = rec / "halted.json"
    path.write_text(json.dumps({"status": status, "sizing_rel": SIZING}), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _later(monkeypatch) -> None:
    """Clock the next mint after a marker written at `now + 1`, without sleeping on mtime granularity."""
    future = time.time() + 10
    monkeypatch.setattr(time, "time", lambda: future)


def _held(root: Path) -> set:
    return {r["run_id"] for r in run_token.held_records(root)}


def test_a_token_minted_before_the_halt_is_not_held_but_still_reclaims(tmp_path):
    root = _repo(tmp_path)
    run_id = _rid(60)
    token = run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert run_id in _held(root)
    _marker(root, run_id, "halted", mtime=time.time() + 5)
    assert run_id not in _held(root)
    assert run_token.halt_admits(root, run_id, token, SCRIPT)
    assert run_token.is_live(root, run_id)


def test_a_refire_remint_after_the_halt_holds_again(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    run_id = _rid(60)
    first = run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    _marker(root, run_id, "halted", mtime=time.time() + 1)
    assert run_id not in _held(root)
    _later(monkeypatch)
    second = run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING, carry_prior=True)
    assert run_id in _held(root)
    assert run_token.halt_admits(root, run_id, first, SCRIPT)
    assert run_token.halt_admits(root, run_id, second, SCRIPT)


@pytest.mark.parametrize("status", ["resumed", "landed"])
def test_a_marker_that_is_not_halted_still_holds(tmp_path, status):
    root = _repo(tmp_path)
    run_id = _rid(60)
    run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    _marker(root, run_id, status, mtime=time.time() + 5)
    assert run_id in _held(root)


def test_a_run_with_no_marker_still_holds(tmp_path):
    root = _repo(tmp_path)
    run_id = _rid(60)
    run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert run_id in _held(root)


def test_a_claimed_record_of_a_halted_run_still_holds(tmp_path):
    root = _repo(tmp_path)
    run_id = _rid(60)
    token = run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    _marker(root, run_id, "halted", mtime=time.time() + 5)
    assert run_token.redeem(root, run_id, token, SCRIPT)
    assert run_id in _held(root)


def test_a_halted_runs_leftover_token_no_longer_vetoes_the_stale_release(tmp_path):
    root = _repo(tmp_path)
    run_id = _rid(3 * 3600)
    run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    _marker(root, run_id, "halted", mtime=time.time() + 5)
    assert hr.stale_release(root, run_id) == hr.RELEASE_STALE_NOT_LIVE


def test_a_live_refire_token_still_vetoes_the_stale_release(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    run_id = _rid(3 * 3600)
    run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    _marker(root, run_id, "halted", mtime=time.time() + 1)
    _later(monkeypatch)
    run_token.mint(root, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING, carry_prior=True)
    assert hr.stale_release(root, run_id) is None


def _stage_repo(tmp_path, monkeypatch):
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL

    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    managed = tmp_path.parent / (tmp_path.name + "-managed")
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    (tmp_path / "scratch" / "warp").mkdir(parents=True)
    (tmp_path / "scratch" / "warp" / "mine.workflow.mjs").write_text("// run script\n", encoding="utf-8")
    return tmp_path, PLAN_REL


def _peer_footprint(root: Path, run_id: str, writes: list) -> None:
    from coordinator_core.ops.dispatch_emit import live_run_writes

    manifest = {"rows": [{"id": "P1", "writes": writes}]}
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    live_run_writes.index_stage(root, run_id, manifest)


def test_a_sibling_overlapping_a_halted_runs_footprint_is_not_parked(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stage_wait
    from coordinator_core.ops.dispatch_emit.ask_stage import stage

    root, plan_rel = _stage_repo(tmp_path, monkeypatch)
    peer = _rid(120)
    run_token.mint(root, peer, SCRIPT, session_id="peer-session")
    _peer_footprint(root, peer, ["pkg/b.py"])
    _marker(root, peer, "halted", mtime=time.time() + 5)
    run_token.mint(root, "mine", SCRIPT, session_id=None)
    assert stage(root, run_id="mine", plan_rel=plan_rel).rows
    assert stage_wait.read(root, "mine") is None


def test_a_sibling_overlapping_a_live_runs_footprint_still_parks(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stage_wait
    from coordinator_core.ops.dispatch_emit.ask_stage import StageWaiting, stage

    root, plan_rel = _stage_repo(tmp_path, monkeypatch)
    peer = _rid(120)
    run_token.mint(root, peer, SCRIPT, session_id="peer-session")
    _peer_footprint(root, peer, ["pkg/b.py"])
    run_token.mint(root, "mine", SCRIPT, session_id="mine-session")
    with pytest.raises(StageWaiting):
        stage(root, run_id="mine", plan_rel=plan_rel)
    assert stage_wait.read(root, "mine") is not None


def _supersedable(root: Path, older: str, newer: str) -> list:
    from coordinator_core.ops.dispatch_emit import land_credited
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    for rid in (older, newer):
        d = root / RECORD_DIR_ROOT / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    return land_credited._superseded_roots(root, newer)


def test_landing_counts_a_halted_runs_pre_halt_token_as_superseded_not_live(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    root = _repo(tmp_path)
    older, newer = _rid(3600), _rid(60)
    run_token.mint(root, older, SCRIPT, session_id="s1", sizing_rel=SIZING)
    _marker(root, older, "halted", mtime=time.time() + 5)
    assert _supersedable(root, older, newer) == [f"{RECORD_DIR_ROOT}/{older}"]


def test_landing_leaves_a_live_earlier_runs_dir_alone(tmp_path):
    root = _repo(tmp_path)
    older, newer = _rid(3600), _rid(60)
    run_token.mint(root, older, SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert _supersedable(root, older, newer) == []
