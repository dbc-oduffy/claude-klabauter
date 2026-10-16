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
    return tmp_path


def _live_peer(root, run_id, writes):
    run_token.mint(root, run_id, SCRIPT, session_id=None)
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "P1", "writes": writes}]}), encoding="utf-8")


def _live(root, run_id):
    return any(r["run_id"] == run_id for r in run_token.live_records(root))


def test_overlap_refused_naming_run_and_paths_and_token_released(repo):
    _live_peer(repo, "peer", ["pkg/b.py"])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(AskStageError, match=r"live-run-write-overlap.*peer: pkg/b\.py"):
        stage(repo, run_id="mine", plan_rel=PLAN_REL)
    assert not _live(repo, "mine")
    assert not (repo / "scratch" / "warp" / "mine" / "commit-request.txt").exists()


def test_disjoint_peer_stages(repo):
    _live_peer(repo, "peer", ["other/z.py"])
    assert sorted(r.id for r in stage(repo, run_id="mine", plan_rel=PLAN_REL).rows) == ["C1", "C2", "C3"]


def test_own_run_footprint_never_counts(repo):
    _live_peer(repo, "mine", ["pkg/a.py"])
    assert stage(repo, run_id="mine", plan_rel=PLAN_REL).rows
    assert _live(repo, "mine")
