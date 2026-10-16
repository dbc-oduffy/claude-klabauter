"""A live peer run's manifest-declared write is a live-peer claim in contested_by_live_peers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.session import core, scope, touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _live_run(root: Path, run_id: str, session: str, writes: list) -> None:
    run_token.mint(root, run_id, "scratch/x.js", session_id=session)
    run_dir = root / "state" / "warp" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps({"rows": [{"writes": writes}]}), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "op.py").write_text("one\n")
    (tmp_path / "other.py").write_text("one\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    for sid in ("mine", "peer"):
        core.init(sid, cwd=str(tmp_path))
    return tmp_path


def test_dirty_manifest_path_without_touch_claim_is_contested(repo):
    _live_run(repo, "run-peer", "peer", ["op.py"])
    (repo / "op.py").write_text("half edit\n")
    got = scope.contested_by_live_peers(["op.py", "other.py"], "mine", str(repo))
    assert got == {"op.py": ["peer"]}


def test_clean_manifest_path_is_not_contested(repo):
    _live_run(repo, "run-peer", "peer", ["op.py"])
    assert scope.contested_by_live_peers(["op.py"], "mine", str(repo)) == {}


def test_own_session_run_does_not_contest(repo):
    _live_run(repo, "run-mine", "mine", ["op.py"])
    (repo / "op.py").write_text("edit\n")
    assert scope.contested_by_live_peers(["op.py"], "mine", str(repo)) == {}


def test_reader_none_makes_result_none(repo, monkeypatch):
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **k: None)
    (repo / "op.py").write_text("edit\n")
    assert scope.contested_by_live_peers(["op.py"], "mine", str(repo)) is None


def test_unparseable_live_manifest_makes_result_none(repo):
    _live_run(repo, "run-peer", "peer", ["op.py"])
    (repo / "state" / "warp" / "run-peer" / "manifest.json").write_text("{not json", encoding="utf-8")
    (repo / "op.py").write_text("edit\n")
    assert scope.contested_by_live_peers(["op.py"], "mine", str(repo)) is None


def test_dead_peer_session_does_not_contest(repo, monkeypatch):
    _live_run(repo, "run-peer", "peer", ["op.py"])
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: False)
    (repo / "op.py").write_text("edit\n")
    assert scope.contested_by_live_peers(["op.py"], "mine", str(repo)) == {}
