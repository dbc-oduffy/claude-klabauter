"""dispatch.ask_stage refuses a sized run whose writes overlap a live run's staged footprint."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, StageWaiting, stage
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


def _mint_peer(root, run_id, *, tokenless, session_id=None, sizing_rel=None):
    """Mint a peer's token. `tokenless=True` mints nothing: the peer is an index entry no engine run
    backs (stale or foreign), so an overlap with it refuses instead of parking."""
    if not tokenless:
        run_token.mint(root, run_id, SCRIPT, session_id=session_id, sizing_rel=sizing_rel)


def _live_peer(root, run_id, writes, *, indexed=True, tokenless=False):
    from coordinator_core.ops.dispatch_emit import live_run_writes

    _mint_peer(root, run_id, tokenless=tokenless)
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"rows": [{"id": "P1", "writes": writes}]}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if indexed:
        live_run_writes.index_stage(root, run_id, manifest)


def _live(root, run_id):
    return any(r["run_id"] == run_id for r in run_token.live_records(root))


def test_overlap_refused_naming_run_and_paths_and_token_released(repo):
    _live_peer(repo, "peer", ["pkg/b.py"], tokenless=True)
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

    _live_peer(repo, "peer", ["pkg/b.py"], tokenless=True)
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

    _live_peer(repo, "peer", ["other/z.py"], indexed=False, tokenless=True)
    assert live_run_index.claim(repo, "peer", ["pkg/b.py"]) == {}
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*peer: pkg/b\.py"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not _live(repo, "mine")


def _idle_peer(root, run_id, writes, *, days, indexed=True, tokenless=False):
    """A staged peer whose owner session `dead-sess` last showed activity `days` days ago."""
    import time
    from datetime import datetime, timezone

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    _mint_peer(root, run_id, tokenless=tokenless, session_id="dead-sess")
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
    _idle_peer(repo, "peer", ["pkg/b.py"], days=3, tokenless=True)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r'live-run-write-overlap.*dispatch\.release_run_token \{"run_id": "peer"\}'):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_recently_active_staged_run_with_dead_owner_does_not_block(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: True)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=0)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows


def test_recently_active_staged_run_with_live_owner_is_refused(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=0, tokenless=True)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def test_halted_peer_with_dead_owner_is_not_treated_as_abandoned(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: True)
    _idle_peer(repo, "peer", ["pkg/b.py"], days=0)
    (warp_live._ask_record_dir(repo, "peer") / "halted.json").write_text('{"status": "paused"}', encoding="utf-8")
    from coordinator_core.ops.dispatch_emit import live_run_index

    assert not live_run_index.run_is_abandoned(repo, "peer")


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
    with pytest.raises(StageWaiting):
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
    with pytest.raises(StageWaiting) as parked:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert parked.value.wait["blockers"] == [PEER_STAMPED]
    stage_wait.clear(repo, "mine")
    stage_wait.park(repo, "mine", [PEER_STAMPED], [], now=1.0)
    with pytest.raises(AskStageError) as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not isinstance(err.value, StageWaiting)
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
    stage_wait.park(repo, "mine", ["peer"], [], now=1.0)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)


def _waits(root):
    home = stage_wait._wait_dir(root)
    return sorted(p.name for p in home.glob("*.json")) if home is not None and home.is_dir() else []


def test_overlap_with_only_live_engine_runs_parks_and_keeps_the_token(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id="sess-1")
    with pytest.raises(StageWaiting, match=r"stage-wait.*peer") as parked:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert parked.value.wait["blockers"] == ["peer"]
    assert 'dispatch.release_run_token {"run_id": "peer"}' in str(parked.value)
    assert _live(repo, "mine")
    assert _waits(repo) == ["mine.json"]
    record = stage_wait.read(repo, "mine")
    assert record["blockers"] == ["peer"] and record["session_id"] == "sess-1" and record["started"] is False
    assert not (repo / "scratch" / "warp" / "mine" / "commit-request.txt").exists()


def test_handler_replies_parked_not_error_and_keeps_the_token(repo):
    from coordinator_core.ops.dispatch_emit import ask_stage

    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    reply = ask_stage._handler({"run_id": "mine", "plan_path": PLAN_REL}, repo_root=repo)
    assert "error" not in reply
    assert reply["parked"] == ask_stage.STAGE_WAIT_PARKED
    assert reply["waiting"]["blockers"] == ["peer"]
    assert 'dispatch.release_run_token {"run_id": "peer"}' in reply["remedy"]
    assert _live(repo, "mine")


def test_repeat_attempt_keeps_one_record_and_the_first_deadline(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    first = stage_wait.read(repo, "mine")["deadline"]
    with pytest.raises(StageWaiting):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert _waits(repo) == ["mine.json"]
    assert stage_wait.read(repo, "mine")["deadline"] == first


def test_any_non_engine_blocker_among_the_hits_refuses_and_consumes(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    _live_peer(repo, "ghost", ["pkg/b.py"], tokenless=True)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*ghost") as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not isinstance(err.value, StageWaiting)
    assert not _live(repo, "mine")
    assert _waits(repo) == []


def test_expired_wait_refuses_consumes_and_clears_the_record(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    stage_wait.park(repo, "mine", ["peer"], [], now=1.0)
    with pytest.raises(AskStageError, match="live-run-write-overlap") as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not isinstance(err.value, StageWaiting)
    assert not _live(repo, "mine")
    assert _waits(repo) == []


def test_blocker_lapsed_by_ttl_refuses_at_the_next_attempt(repo, monkeypatch):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    monkeypatch.setattr(run_token, "is_live", lambda root, rid: rid != "peer")
    with pytest.raises(AskStageError, match="live-run-write-overlap") as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not isinstance(err.value, StageWaiting)
    assert _waits(repo) == []


def test_attempt_after_the_blocker_ended_stages_and_drops_the_wait(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    run_token.discard(repo, "peer")
    assert not _live(repo, "mine")  # the blocker's end retired the waiter's token and spooled its refire
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows
    assert _waits(repo) == []
    assert _live(repo, "mine")


def test_repark_after_a_started_wait_can_be_woken_again(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "peer")] == ["mine"]
    with pytest.raises(StageWaiting):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert stage_wait.read(repo, "mine")["started"] is False
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "peer")] == ["mine"]


def _spend(root, run_id):
    """Write the sentinel a spent-token gate refusal leaves, without dropping the run's claims."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "token-spent.json").write_text("{}\n", encoding="utf-8")


def test_spent_token_peer_with_claims_does_not_block_a_sibling(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_run_index

    monkeypatch.setattr(warp_live, "_owner_exited", lambda sid, minted: False)
    _live_peer(repo, "peer", ["pkg/b.py"], indexed=False, tokenless=True)
    assert live_run_index.claim(repo, "peer", ["pkg/b.py"]) == {}
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match="live-run-write-overlap"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    _spend(repo, "peer")
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows


SIZING = "state/sizings/2026-11/chain-a.yaml"


def _claiming_run(root, run_id, sizing, path="pkg/b.py", *, tokenless=False):
    from coordinator_core.ops.dispatch_emit import live_run_index

    _mint_peer(root, run_id, tokenless=tokenless, sizing_rel=sizing)
    assert live_run_index.claim(root, run_id, [path]) == {}


def test_chained_run_is_not_refused_by_its_ancestors_claims(repo):
    _claiming_run(repo, "ask-20261110T020544-281faa", SIZING)
    run_token.mint(repo, "mine", SCRIPT, session_id=None, sizing_rel=SIZING)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows
    assert _live(repo, "mine")


def test_unrelated_sizing_peer_still_refuses_a_chained_run(repo):
    _claiming_run(repo, "ask-20261110T020544-281faa", SIZING, path="pkg/a.py")
    _claiming_run(repo, "peer", "state/sizings/2026-11/other.yaml", tokenless=True)
    run_token.mint(repo, "mine", SCRIPT, session_id=None, sizing_rel=SIZING)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*peer: pkg/b\.py") as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert "281faa" not in str(err.value)


def test_marking_a_token_spent_drops_the_runs_index_entry_and_claims(repo):
    from coordinator_core.ops.dispatch_emit import ask_gate, live_run_index

    _live_peer(repo, "peer", ["pkg/b.py"], indexed=False)
    assert live_run_index.claim(repo, "peer", ["pkg/b.py"]) == {}
    ask_gate._mark_token_spent(repo, "peer")
    assert live_run_index.release(repo, "peer") == 0
    assert "peer" not in (live_run_index.read(repo, 2000.0) or {})
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows


def _plan_with_dir_write(repo):
    (repo / "tools" / "proto").mkdir(parents=True)
    plan = PLAN.replace("    - pkg/c.py\n", "    - pkg/c.py\n    - tools/proto\n")
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")


def test_declared_directory_does_not_claim_a_peers_files_under_it(repo):
    _plan_with_dir_write(repo)
    _live_peer(repo, "peer", ["tools/proto/launcher.py"])
    assert sorted(r.id for r in stage(repo, run_id="mine", plan_rel=PLAN_REL).rows) == ["C1", "C2", "C3"]


def test_run_declaring_a_directory_still_refuses_on_a_shared_file(repo):
    _plan_with_dir_write(repo)
    _live_peer(repo, "peer", ["tools/proto/launcher.py", "pkg/c.py"], tokenless=True)
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*peer: pkg/c\.py") as err:
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert "launcher.py" not in str(err.value)
