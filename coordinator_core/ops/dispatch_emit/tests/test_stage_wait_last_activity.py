"""A blocker whose stage stamp is old but whose run artifacts are recent is reported live, with its last-activity time."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, StageWaiting, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_live_overlap import (  # noqa: F401 - repo is a fixture
    SCRIPT,
    _idle_peer,
    repo,
)


def _fresh_artifact(root, run_id, *, age_s):
    """Age the peer's manifest (a run artifact) to `age_s` seconds ago; returns that epoch."""
    target = root / "scratch" / "warp" / run_id / "manifest.json"
    epoch = float(int(time.time() - age_s))
    os.utime(target, (epoch, epoch))
    return epoch


def test_park_record_carries_artifact_time_despite_old_stage_stamp(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3)
    artifact = _fresh_artifact(repo, "peer", age_s=90)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting) as parked:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert parked.value.wait["last_activity"] == {"peer": stage_wait.iso_utc(artifact)}
    assert stage_wait.read(repo, "mine")["last_activity"] == {"peer": stage_wait.iso_utc(artifact)}
    assert stage_wait.iso_utc(artifact) in str(parked.value)


def test_refusal_names_last_activity_next_to_the_run_before_the_remedy(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3, tokenless=True)
    artifact = _fresh_artifact(repo, "peer", age_s=90)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError) as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    text = str(err.value)
    assert not isinstance(err.value, StageWaiting)
    stamp = stage_wait.iso_utc(artifact)
    assert f"peer: pkg/b.py (last activity {stamp})" in text
    assert text.index(stamp) < text.index("dispatch.release_run_token")
    assert "only if its last activity is old" in text


def test_blocker_without_any_activity_signal_reads_unknown(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_stage

    monkeypatch.setattr(ask_stage, "_blockers_last_activity", lambda root, rids: {})
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3, tokenless=True)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"peer: pkg/b\.py \(last activity unknown\)"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_park_without_activity_keeps_an_empty_map(repo):
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    record = stage_wait.park(repo, "mine", ["peer"], [], now=1.0)
    assert record["last_activity"] == {}
