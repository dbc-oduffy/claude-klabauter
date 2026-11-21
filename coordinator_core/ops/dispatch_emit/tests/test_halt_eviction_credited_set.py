"""Eviction parks only the evicting run's declared or touch-recorded paths. A path the tree mtime
sweep found (a peer run's live dirt, never declared by this run) stays dirty in the worktree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halt_tree_check
from coordinator_core.ops.dispatch_emit.halted_record_eviction import plan_tree_eviction


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HEAD = {"own.py": "X = 1\n", "peer.py": "Y = 1\n"}
DIRTY = {"own.py": "X = 1\nA = 2\n", "peer.py": "Y = 1\nB = 3\n"}


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    for rel, text in HEAD.items():
        (root / rel).write_bytes(text.encode())
    subprocess.run(["git", "add", *HEAD], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "head"], cwd=root, check=True,
    )
    common = resolve_git_common_dir(root)
    baseline = {rel: git_objects.write_object(common, b"blob", HEAD[rel].encode()) for rel in HEAD}
    (root / "state" / "warp" / "r1").mkdir(parents=True)
    (root / "state" / "warp" / "r1" / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": baseline}), encoding="utf-8"
    )
    for rel, text in DIRTY.items():
        (root / rel).write_bytes(text.encode())
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {})
    return root


def _plan(root: Path, credited):
    paths = list(DIRTY)
    return plan_tree_eviction(root, "state/warp/r1", paths, set(paths), paths, {}, [], run_id="r1", credited=credited)


def test_uncredited_peer_path_is_never_parked_or_reset(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch)
    ev = _plan(root, credited={"own.py"})
    assert ev is not None
    ev.apply()
    assert (root / "own.py").read_text() == HEAD["own.py"]
    assert (root / "peer.py").read_text() == DIRTY["peer.py"]
    assert ev.evicted == ["own.py"]


def test_nothing_credited_parks_nothing(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch)
    assert _plan(root, credited=set()) is None
    assert (root / "peer.py").read_text() == DIRTY["peer.py"]
