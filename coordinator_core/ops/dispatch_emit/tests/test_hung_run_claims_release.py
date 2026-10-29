"""A held run left at stage `minted` with no recent activity releases its declared paths to another
run's `review_prep.declared_paths_for_run` and records a halted outcome; a run with recent activity
still withholds them."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep, warp_run_deliverable
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
PATH = "src/held.py"


def _stamp(age_seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    session_core.init(PEER, cwd=str(root))
    own = record_dir(root, "run-own")
    own.mkdir(parents=True)
    (own / "manifest.json").write_text(json.dumps({"review_declared_paths": [PATH]}), encoding="utf-8")
    return root


def _peer(repo: Path, *, stage: str | None, age: float = 0.0) -> Path:
    """Run `run-peer`: a live token held by PEER, its manifest declaring PATH, and (when `stage`) a
    run-stage record last active `age` seconds ago."""
    peer = record_dir(repo, "run-peer")
    peer.mkdir(parents=True)
    (peer / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}), encoding="utf-8"
    )
    run_token.mint(repo, "run-peer", "peer.mjs", session_id=PEER)
    if stage:
        write_run_stage_at(peer, stage, now=_stamp(age))
    return peer


def _paths(repo: Path):
    return review_prep.declared_paths_for_run(repo, "run-own", own_session=SELF)


def test_a_peer_stale_at_minted_releases_its_paths_and_is_halted(repo):
    peer = _peer(repo, stage="minted", age=warp_run_deliverable.MINTED_STALE_SECONDS + 600)
    assert _paths(repo) == [PATH]
    assert warp_run_deliverable._halted(repo, "run-peer")
    marker = json.loads((peer / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == warp_run_deliverable.ABANDONED_AT_MINTED
    assert "minted" in marker["reason"]


def test_a_peer_with_recent_activity_at_minted_still_withholds(repo):
    peer = _peer(repo, stage="minted", age=60)
    with pytest.raises(ValueError, match="no reviewable paths"):
        _paths(repo)
    assert not (peer / "halted.json").exists()


def test_a_peer_stale_past_minted_is_not_released(repo):
    _peer(repo, stage="staged", age=warp_run_deliverable.MINTED_STALE_SECONDS + 600)
    with pytest.raises(ValueError, match="no reviewable paths"):
        _paths(repo)


def test_a_peer_with_no_stage_record_still_withholds(repo):
    _peer(repo, stage=None)
    with pytest.raises(ValueError, match="no reviewable paths"):
        _paths(repo)


def test_a_minted_peer_with_no_parseable_stamp_still_withholds(repo):
    peer = _peer(repo, stage="minted", age=0)
    stage_file = peer / "run-stage.json"
    data = json.loads(stage_file.read_text(encoding="utf-8"))
    data["last_activity_at"] = "not-a-time"
    data.pop("started_at", None)
    stage_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="no reviewable paths"):
        _paths(repo)
