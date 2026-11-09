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


def test_unclaimed_falls_back_to_readonly_check_and_still_refuses(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    _live_peer(repo, "peer", ["pkg/b.py"], indexed=False)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    monkeypatch.setattr(live_run_index, "claim", lambda *a, **k: None)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
