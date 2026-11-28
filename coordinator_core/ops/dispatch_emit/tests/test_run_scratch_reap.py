"""Tests for run_scratch_reap.reap_run: landed/halted gate, keeps, peers, tracked paths."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core import owner_mark, temp_layout
from coordinator_core.ops.dispatch_emit import run_scratch, run_scratch_reap

RUN = "ask-20261109T000000-aaaaaa"
PEER = "ask-20261109T000001-bbbbbb"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def _record(root: Path, run_id: str, marker=None) -> Path:
    rec = root / "state" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    if marker is not None:
        (rec / "halted.json").write_text(json.dumps({"status": marker}), encoding="utf-8")
    return rec


def _scratch(root: Path, rel: str) -> Path:
    p = root / rel
    p.mkdir(parents=True, exist_ok=True)
    (p / "f.txt").write_text("x", encoding="utf-8")
    return p


def _git_index(root: Path, tracked: bytes = b"") -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "index").write_bytes(b"DIRC" + (2).to_bytes(4, "big") + tracked)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git_index(tmp_path)
    return tmp_path


def _tombstones(root: Path) -> list[str]:
    tomb = root / "scratch" / ".deleted"
    return sorted(p.name for p in tomb.iterdir()) if tomb.is_dir() else []


def test_landed_reaped(repo):
    _record(repo, RUN, "landed")
    _scratch(repo, f"scratch/warp/{RUN}")
    _scratch(repo, "scratch/land-one")
    run_scratch.record_paths(repo, RUN, ["scratch/land-one"])
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert sorted(out["removed"]) == sorted([f"scratch/warp/{RUN}", "scratch/land-one"])
    assert not (repo / "scratch" / "land-one").exists()
    assert len(_tombstones(repo)) == 2


def test_never_halted_run_is_reaped_despite_fresh_activity(repo):
    rec = _record(repo, RUN)
    (rec / "run-stage.json").write_text(
        json.dumps({"last_activity_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )
    _scratch(repo, f"scratch/warp/{RUN}")
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [f"scratch/warp/{RUN}"]


def _warp_run(root: Path, run_id: str = RUN) -> Path:
    """A run's warp dir: the export trees and briefs a resume rebuilds, plus every resume input."""
    warp = root / "scratch" / "warp" / run_id
    for tree in ("export", "export-head", "export.base", "briefs"):
        _scratch(root, f"scratch/warp/{run_id}/{tree}")
    for name in run_scratch_reap.RESUME_INPUTS:
        (warp / name).write_text("x", encoding="utf-8")
    (warp / "terminal-commit-params.json").write_text("x", encoding="utf-8")
    return warp


def _left(warp: Path) -> set[str]:
    return {p.name for p in warp.iterdir()}


@pytest.mark.parametrize("status", [None, "landed", "superseded", "closed"])
def test_terminal_run_leaves_no_warp_tree(repo, status):
    _record(repo, RUN, status)
    warp = _warp_run(repo)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [f"scratch/warp/{RUN}"] and out["held"] == ""
    assert not warp.exists()


@pytest.mark.parametrize("status", ["halted", "resumed"])
def test_resumable_halt_keeps_exactly_its_resume_inputs(repo, status):
    _record(repo, RUN, status)
    warp = _warp_run(repo)
    _scratch(repo, "scratch/land-one")
    run_scratch.record_paths(repo, RUN, ["scratch/land-one"])
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert _left(warp) == set(run_scratch_reap.RESUME_INPUTS)
    assert sorted(out["removed"]) == sorted(
        [f"scratch/warp/{RUN}/{n}" for n in ("export", "export-head", "export.base", "briefs", "terminal-commit-params.json")]
        + ["scratch/land-one"]
    )
    assert out["held"] == "" and not (repo / "scratch" / "land-one").exists()


def test_resumable_halt_reap_is_idempotent(repo):
    _record(repo, RUN, "halted")
    warp = _warp_run(repo)
    run_scratch_reap.reap_run(repo, RUN, NOW)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and _left(warp) == set(run_scratch_reap.RESUME_INPUTS)


def test_unknown_halt_status_holds_whole_tree(repo):
    _record(repo, RUN, "mystery")
    warp = _warp_run(repo)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and out["held"] and (warp / "export").is_dir()


def test_halt_record_inside_warp_dir_holds_whole_tree(repo):
    warp = _warp_run(repo)
    (warp / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    (warp / "run.json").write_text("{}", encoding="utf-8")
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and out["held"] and (warp / "export").is_dir()


def test_resumable_halt_live_retest_holds(repo, monkeypatch):
    _record(repo, RUN, "halted")
    warp = _warp_run(repo)
    monkeypatch.setattr(run_scratch_reap.export_lifecycle, "_retest_live", lambda r, i: True)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and out["held"] and (warp / "export").is_dir()


def test_resumable_halt_spares_unexpired_keep_under_warp(repo):
    _record(repo, RUN, "halted")
    warp = _warp_run(repo)
    run_scratch.declare_keep(repo, RUN, f"scratch/warp/{RUN}/export", "repro", 2, now=NOW)
    out = run_scratch_reap.reap_run(repo, RUN, NOW + timedelta(hours=1))
    assert (warp / "export").is_dir() and not (warp / "briefs").exists()
    assert f"scratch/warp/{RUN}/export" in out["kept"]


def test_superseded_reaped(repo):
    _record(repo, RUN, "superseded")
    _scratch(repo, "scratch/probe-one")
    run_scratch.record_paths(repo, RUN, ["scratch/probe-one"])
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == ["scratch/probe-one"]


def test_unexpired_keep_kept_expired_keep_reaped(repo):
    _record(repo, RUN, "landed")
    _scratch(repo, "scratch/probe-keep")
    run_scratch.declare_keep(repo, RUN, "scratch/probe-keep", "repro for bug", 2, now=NOW)
    out = run_scratch_reap.reap_run(repo, RUN, NOW + timedelta(hours=1))
    assert "scratch/probe-keep" in out["kept"] and (repo / "scratch" / "probe-keep").is_dir()
    out = run_scratch_reap.reap_run(repo, RUN, NOW + timedelta(hours=3))
    assert "scratch/probe-keep" in out["removed"]
    assert "scratch/probe-keep" not in run_scratch.read_keep_index(repo)


def test_expired_peer_keep_reaped_only_when_peer_unreferenced(repo):
    _record(repo, RUN, "landed")
    _record(repo, PEER, "landed")
    _scratch(repo, "scratch/probe-peer")
    run_scratch.declare_keep(repo, PEER, "scratch/probe-peer", "peer repro", 1, now=NOW)
    out = run_scratch_reap.reap_run(repo, RUN, NOW + timedelta(hours=2))
    assert "scratch/probe-peer" in out["removed"]


def test_peer_listed_untouched(repo):
    _record(repo, RUN, "landed")
    _record(repo, PEER, "halted")
    _scratch(repo, "scratch/land-shared")
    _scratch(repo, f"scratch/warp/{PEER}")
    run_scratch.declare_keep(repo, PEER, "scratch/land-shared", "peer", 1, now=NOW)
    run_scratch.record_paths(repo, RUN, ["scratch/land-shared", f"scratch/warp/{PEER}"])
    out = run_scratch_reap.reap_run(repo, RUN, NOW + timedelta(hours=5))
    assert (repo / "scratch" / "land-shared").is_dir()
    assert (repo / "scratch" / "warp" / PEER).is_dir()
    assert "scratch/land-shared" in out["kept"] and f"scratch/warp/{PEER}" in out["kept"]


def test_git_tracked_refused(repo):
    _git_index(repo, b"scratch/land-tracked/f.txt")
    _record(repo, RUN, "landed")
    _scratch(repo, "scratch/land-tracked")
    run_scratch.record_paths(repo, RUN, ["scratch/land-tracked"])
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert (repo / "scratch" / "land-tracked").is_dir()
    assert [r["path"] for r in out["refused"]] == ["scratch/land-tracked"]


def test_live_retest_holds_run(repo, monkeypatch):
    _record(repo, RUN, "landed")
    _scratch(repo, "scratch/land-one")
    run_scratch.record_paths(repo, RUN, ["scratch/land-one"])
    monkeypatch.setattr(run_scratch_reap.export_lifecycle, "_retest_live", lambda r, i: True)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and (repo / "scratch" / "land-one").is_dir()


def test_unreadable_ledger_reaps_nothing_and_reports(repo):
    rec = _record(repo, RUN, "landed")
    (rec / run_scratch.LEDGER_FILE).write_text("{not json", encoding="utf-8")
    _scratch(repo, f"scratch/warp/{RUN}")
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and out["refused"]
    assert (repo / "scratch" / "warp" / RUN).is_dir()


def test_unledgered_path_never_deleted(repo):
    _record(repo, RUN, "landed")
    _scratch(repo, "scratch/land-unlisted")
    run_scratch_reap.reap_run(repo, RUN, NOW)
    assert (repo / "scratch" / "land-unlisted").is_dir()


def test_share_dir_review_prep_sidecar_tombstoned_peer_sidecar_untouched(repo):
    _record(repo, RUN, "landed")
    share = repo / ".coordinator-local" / "subagent-share" / "sess-1"
    share.mkdir(parents=True)
    mine = share / "p-1.review-prep.md"
    theirs = share / "p-2.review-prep.md"
    mine.write_text("m", encoding="utf-8")
    theirs.write_text("t", encoding="utf-8")
    rel = ".coordinator-local/subagent-share/sess-1/p-1.review-prep.md"
    run_scratch.record_paths(repo, RUN, [rel])
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert rel in out["removed"]
    assert not mine.exists() and theirs.read_text(encoding="utf-8") == "t"
    assert any(n.endswith("p-1.review-prep.md") for n in _tombstones(repo))


def test_no_spawn_and_no_listing(repo, monkeypatch):
    _record(repo, RUN, "landed")
    for i in range(5):
        _scratch(repo, f"scratch/land-{i}")
    run_scratch.record_paths(repo, RUN, [f"scratch/land-{i}" for i in range(5)])

    def boom(*a, **k):
        raise AssertionError("forbidden call")

    real_scandir = os.scandir
    listed: list[str] = []

    def writer_roots_only(path):
        listed.append(os.path.normcase(os.path.normpath(str(path))))
        return real_scandir(path)

    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", boom)
        for name in ("listdir", "walk"):
            m.setattr(os, name, boom)
        m.setattr(os, "scandir", writer_roots_only)
        out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert len(out["removed"]) == 5
    allowed = {
        os.path.normcase(os.path.normpath(str(repo.joinpath(*s.split("/")))))
        for s in run_scratch_reap.OWNED_REPO_ROOTS
    }
    allowed.add(os.path.normcase(os.path.normpath(str(temp_layout.coordinator_temp_root(repo)))))
    assert set(listed) <= allowed


def _owned(parent: Path, name: str, owner_id: str, repo: Path, kind: str = "run") -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / "f.txt").write_text("x", encoding="utf-8")
    owner_mark.write_mark(d, kind, owner_id, repo=repo)
    return d


@pytest.fixture
def no_keep(monkeypatch):
    monkeypatch.setattr(owner_mark, "keep_paths", lambda: {})


def test_owner_marked_share_session_and_temp_paths_reaped_ledger_absent(repo, monkeypatch, no_keep, tmp_path_factory):
    _record(repo, RUN, "landed")
    share = repo / ".coordinator-local" / "subagent-share"
    sess = repo / ".coordinator-local" / "coordinator-sessions"
    temp = tmp_path_factory.mktemp("coord-temp")
    monkeypatch.setattr(temp_layout, "coordinator_temp_root", lambda r: temp)
    mine_share = _owned(share, "run-share", RUN, repo)
    mine_sess = _owned(sess, "run-sess", RUN, repo)
    mine_idx = _owned(temp, "git-index-1", RUN, repo)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert not mine_share.exists() and not mine_sess.exists() and not mine_idx.exists()
    assert ".coordinator-local/subagent-share/run-share" in out["removed"]
    assert ".coordinator-local/coordinator-sessions/run-sess" in out["removed"]
    assert str(mine_idx) in out["removed"]
    assert len(_tombstones(repo)) == 3


def test_external_dir_on_another_volume_is_renamed_beside_itself_and_unmarked(repo, monkeypatch, no_keep, tmp_path_factory):
    _record(repo, RUN, "landed")
    temp = tmp_path_factory.mktemp("coord-temp")
    monkeypatch.setattr(temp_layout, "coordinator_temp_root", lambda r: temp)
    mine = _owned(temp, "git-index-1", RUN, repo)
    real_rename = os.rename

    def cross_volume(src, dst):
        if ".deleted-" not in os.path.basename(str(dst)):
            raise OSError("cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", cross_volume)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert not mine.exists() and out["refused"] == [] and str(mine) in out["removed"]
    beside = [p for p in temp.iterdir() if p.name.startswith(".deleted-")]
    assert len(beside) == 1 and not (beside[0] / owner_mark.MARK_FILE).exists()


def test_owner_marks_of_peer_run_session_owner_and_unmarked_dirs_survive(repo, monkeypatch, no_keep, tmp_path_factory):
    _record(repo, RUN, "landed")
    share = repo / ".coordinator-local" / "subagent-share"
    temp = tmp_path_factory.mktemp("coord-temp")
    monkeypatch.setattr(temp_layout, "coordinator_temp_root", lambda r: temp)
    peer = _owned(share, "peer", PEER, repo)
    sess_owned = _owned(share, "sess", RUN, repo, kind="session")
    other_repo = _owned(share, "elsewhere", RUN, repo / "other")
    plain = share / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x", encoding="utf-8")
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert peer.exists() and sess_owned.exists() and other_repo.exists() and plain.exists()
    assert out["removed"] == []


def test_owned_paths_survive_a_resumable_halt_and_a_live_retest(repo, monkeypatch, no_keep, tmp_path_factory):
    _record(repo, RUN, "halted")
    monkeypatch.setattr(temp_layout, "coordinator_temp_root", lambda r: tmp_path_factory.mktemp("t"))
    mine = _owned(repo / ".coordinator-local" / "subagent-share", "run-share", RUN, repo)
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert mine.exists()
    assert ".coordinator-local/subagent-share/run-share" in out["kept"]


def test_keep_set_member_under_a_run_mark_is_not_tombstoned(repo, monkeypatch, tmp_path_factory):
    _record(repo, RUN, "landed")
    temp = tmp_path_factory.mktemp("coord-temp")
    monkeypatch.setattr(temp_layout, "coordinator_temp_root", lambda r: temp)
    engine = _owned(temp, "claude-klabauter-publish-engine", RUN, repo)
    monkeypatch.setattr(owner_mark, "keep_paths", lambda: {"publish-engine": [engine]})
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert engine.exists() and str(engine) in out["kept"]


def test_owned_link_is_never_followed(repo, no_keep, monkeypatch, tmp_path_factory):
    _record(repo, RUN, "landed")
    monkeypatch.setattr(temp_layout, "coordinator_temp_root", lambda r: tmp_path_factory.mktemp("t"))
    target = tmp_path_factory.mktemp("elsewhere")
    owner_mark.write_mark(target, "run", RUN, repo=repo)
    share = repo / ".coordinator-local" / "subagent-share"
    share.mkdir(parents=True)
    try:
        os.symlink(target, share / "lnk", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted")
    run_scratch_reap.reap_run(repo, RUN, NOW)
    assert target.exists() and (target / owner_mark.MARK_FILE).exists()


def test_halted_run_landed_close_queues_scratch_reap():
    from coordinator_core.ops.dispatch_emit import halted_run_landed

    assert "scratch-reap" in halted_run_landed.CLOSE_LEGS
