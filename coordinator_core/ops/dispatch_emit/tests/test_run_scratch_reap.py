"""Tests for run_scratch_reap.reap_run: landed/halted gate, keeps, peers, tracked paths."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def test_halted_kept(repo):
    _record(repo, RUN, "halted")
    _scratch(repo, "scratch/land-one")
    run_scratch.record_paths(repo, RUN, ["scratch/land-one"])
    out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert out["removed"] == [] and "scratch/land-one" in out["kept"] and out["held"]
    assert (repo / "scratch" / "land-one").is_dir()


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

    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", boom)
        for name in ("scandir", "listdir", "walk"):
            m.setattr(os, name, boom)
        out = run_scratch_reap.reap_run(repo, RUN, NOW)
    assert len(out["removed"]) == 5
