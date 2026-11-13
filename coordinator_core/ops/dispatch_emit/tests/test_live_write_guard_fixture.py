"""End to end: a halt eviction whose HEAD-based bytes would drop a peer run's unlanded hunk is refused naming the peer."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_evict

pytestmark = pytest.mark.spawns_process

PATH = "pkg/mod.py"
HEAD = b"a = 1\nb = 2\nc = 3\nd = 4\n"
PEER_LIVE = b"a = 1\nb = 2\n# peer hunk one\npeer_two = 2\nc = 3\nd = 4\n"
OWN = "ask-own"


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.strip()


def _world(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / PATH).write_bytes(HEAD)
    _git(root, "add", PATH)
    _git(root, "commit", "-q", "-m", "base")
    base_sha = _git(root, "rev-parse", f"HEAD:{PATH}")
    peer = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-aaaaaa"
    rdir = root / "state" / "warp" / peer
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: base_sha}}),
        encoding="utf-8",
    )
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {PATH: "left in tree"}}}), encoding="utf-8"
    )
    (root / PATH).write_bytes(PEER_LIVE)
    return root, tmp_path / "rec", peer


def _stage_own(root: Path, baseline: bytes) -> None:
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    sha = git_objects.write_object(resolve_git_common_dir(root), b"blob", baseline)
    rdir = root / "state" / "warp" / OWN
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": {PATH: sha}}), encoding="utf-8"
    )


def test_a_reset_that_would_drop_peer_hunks_is_refused_and_peer_hunks_survive(tmp_path, monkeypatch):
    root, rec, peer = _world(tmp_path)
    monkeypatch.setattr(halt_tree_evict, "_reset_plan", lambda *a: (HEAD, None))
    refused: dict = {}
    index = halt_tree_evict.evict(root, rec, [PATH], {PATH: HEAD}, run_id=OWN, refused=refused)
    assert peer in refused[PATH]
    assert index == {}
    assert (root / PATH).read_bytes() == PEER_LIVE
    assert not (rec / halt_tree_evict.EVICTED_FILE).exists()


def test_evict_over_a_hunk_the_peer_delta_also_covers_is_refused_and_tree_kept(tmp_path):
    root, rec, _peer = _world(tmp_path)
    mine = PEER_LIVE + b"mine = 1\n"
    (root / PATH).write_bytes(mine)
    _stage_own(root, PEER_LIVE)
    refused: dict = {}
    index = halt_tree_evict.evict(root, rec, [PATH], {PATH: HEAD}, run_id=OWN, refused=refused)
    assert PATH in refused
    assert index == {}
    assert (root / PATH).read_bytes() == mine
