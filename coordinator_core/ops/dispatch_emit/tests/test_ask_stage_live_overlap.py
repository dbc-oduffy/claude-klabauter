"""dispatch.ask_stage refuses a sized run whose writes overlap a live run's staged footprint."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL

SCRIPT = "scratch/warp/x.mjs"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    managed = tmp_path.parent / (tmp_path.name + "-managed")
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    # ensure_twin refuses a run manifest whose run script is absent; a slotless script is the no-twin path.
    (tmp_path / "scratch" / "warp").mkdir(parents=True)
    (tmp_path / "scratch" / "warp" / "mine.workflow.mjs").write_text("// run script\n", encoding="utf-8")
    return tmp_path


def _live_peer(root, run_id, writes, *, indexed=True):
    from coordinator_core.ops.dispatch_emit import live_run_writes

    run_token.mint(root, run_id, SCRIPT, session_id=None)
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"rows": [{"id": "P1", "writes": writes}]}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if indexed:
        live_run_writes.index_stage(root, run_id, manifest)


def _live(root, run_id):
    return any(r["run_id"] == run_id for r in run_token.live_records(root))


def test_overlap_refused_naming_run_and_paths_and_token_released(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*peer: pkg/b\.py"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not _live(repo, "mine")
    assert not (repo / "scratch" / "warp" / "mine" / "commit-request.txt").exists()


def test_stalled_footprint_read_degrades_and_stages(repo, monkeypatch, caplog):
    import threading

    from coordinator_core.ops.dispatch_emit import ask_stage, live_run_index, live_run_writes

    _live_peer(repo, "peer", ["pkg/b.py"])
    gate = threading.Event()
    monkeypatch.setattr(live_run_index, "claim", lambda *a, **k: None)
    monkeypatch.setattr(live_run_writes, "live_footprints", lambda *a, **k: gate.wait(10))
    monkeypatch.setattr(ask_stage, "LIVE_OVERLAP_DEADLINE_S", 0.05)
    try:
        with caplog.at_level("WARNING"):
            assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows
    finally:
        gate.set()
    assert "check skipped" in caplog.text


def test_fast_footprint_read_still_refuses_under_deadline(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_stage

    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    monkeypatch.setattr(ask_stage, "LIVE_OVERLAP_DEADLINE_S", 5.0)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_disjoint_peer_stages(repo):
    _live_peer(repo, "peer", ["other/z.py"])
    assert sorted(r.id for r in stage(repo, run_id="mine", plan_rel=PLAN_REL).rows) == ["C1", "C2", "C3"]


def test_own_run_footprint_never_counts(repo):
    _live_peer(repo, "mine", ["pkg/a.py"])
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows
    assert _live(repo, "mine")


def test_claim_is_the_default_path_and_records_this_runs_footprint(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    seen = []
    real = live_run_index.claim
    monkeypatch.setattr(live_run_index, "claim", lambda root, rid, w: seen.append(rid) or real(root, rid, w))
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows
    assert seen == ["mine"]


def test_claimed_but_unstaged_peer_blocks(repo):
    from coordinator_core.ops.dispatch_emit import live_run_index

    _live_peer(repo, "peer", ["other/z.py"], indexed=False)
    assert live_run_index.claim(repo, "peer", ["pkg/b.py"]) == {}
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*peer: pkg/b\.py"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not _live(repo, "mine")


def _idle_peer(root, run_id, writes, *, days, indexed=True):
    """A staged peer whose owner session `dead-sess` last showed activity `days` days ago."""
    import time
    from datetime import datetime, timezone

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    run_token.mint(root, run_id, SCRIPT, session_id="dead-sess")
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"rows": [{"id": "P1", "writes": writes}]}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if indexed:
        from coordinator_core.ops.dispatch_emit import live_run_writes

        live_run_writes.index_stage(root, run_id, manifest)
    stamp = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = warp_live._ask_record_dir(root, run_id)
    record.mkdir(parents=True, exist_ok=True)
    (record / RUN_STAGE_FILE).write_text(
        json.dumps({"stage": "staged", "staged_at": stamp, "last_activity_at": stamp}), encoding="utf-8"
    )


@pytest.mark.parametrize("peer", ["ask-20261107T023814-8161a4", "ask-20261106T020647-5e9552"])
def test_staged_run_days_idle_with_dead_owner_does_not_block(repo, monkeypatch, peer):
    from coordinator_core.ops.dispatch_emit import live_run_index

    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: sid == "dead-sess")
    _idle_peer(repo, peer, ["pkg/b.py"], days=3)
    assert live_run_index.claim(repo, peer, ["pkg/b.py"]) == {}
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows


def test_idle_run_with_live_owner_is_refused_naming_the_release_op(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r'live-run-write-overlap.*dispatch\.release_run_token \{"run_id": "peer"\}'):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_recently_active_run_with_dead_owner_is_refused(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: True)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=0)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_stale_peer_does_not_block_the_readonly_fallback(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: True)
    monkeypatch.setattr(live_run_index, "claim", lambda *a, **k: None)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3, indexed=False)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows


def test_release_op_clears_the_blocking_claim_markers(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index, release_run_token

    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3, indexed=False)
    assert live_run_index.claim(repo, "peer", ["pkg/b.py"]) == {}
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    reply = release_run_token._handler({"run_id": "peer"}, repo_root=repo)
    assert reply["claim_markers_removed"] == 1
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows


PEER_STAMPED = "warp-2026-11-04-peer-a70f48-20261104T150507-3bf668"
PEER_MISSTAMPED = "warp-2026-11-04-peer-a70f48-20261109T150507-3bf668"


def test_refusal_prints_the_recorded_run_id_not_a_misstamped_one(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    _live_peer(repo, PEER_STAMPED, ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    monkeypatch.setattr(live_run_index, "claim", lambda *a, **k: {PEER_MISSTAMPED: ["pkg/b.py"]})
    with pytest.raises(AskStageError) as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert PEER_STAMPED in str(err.value)
    assert PEER_MISSTAMPED not in str(err.value)
    assert f'dispatch.release_run_token {{"run_id": "{PEER_STAMPED}"}}' in str(err.value)


def test_refusal_keeps_an_unrecorded_hit_id_as_given(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    monkeypatch.setattr(live_run_index, "claim", lambda *a, **k: {PEER_MISSTAMPED: ["pkg/b.py"]})
    with pytest.raises(AskStageError, match=PEER_MISSTAMPED):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_unclaimed_falls_back_to_readonly_check_and_still_refuses(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    _live_peer(repo, "peer", ["pkg/b.py"], indexed=False)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    monkeypatch.setattr(live_run_index, "claim", lambda *a, **k: None)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
