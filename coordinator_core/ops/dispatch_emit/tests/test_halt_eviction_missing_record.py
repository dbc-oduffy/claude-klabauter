"""A record path ``plan_tree_eviction`` appended but ``apply`` never wrote is dropped from ``paths``; the tree is never written."""

from __future__ import annotations

import subprocess

from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict
from coordinator_core.ops.dispatch_emit.halted_record_eviction import plan_tree_eviction
from pathlib import Path
import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REC = "state/warp/run-x"
BAD = b"broken\n"


def _plan(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    (root / "mod.py").write_bytes(BAD)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {p: "x" for p in own})
    paths: list = ["other.txt"]
    ev = plan_tree_eviction(root, REC, ["mod.py"], {"mod.py"}, ["mod.py"], {}, paths)
    return root, ev, paths


def test_parked_path_gone_before_apply_drops_unwritten_record_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, ev, paths = _plan(tmp_path, monkeypatch)
    assert ev is not None
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths
    (root / "mod.py").unlink()
    ev.apply()
    assert paths == ["other.txt"]
    assert not (root / REC / halt_tree_evict.EVICTED_FILE).exists()


def test_written_record_paths_stay_and_the_tree_keeps_the_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, ev, paths = _plan(tmp_path, monkeypatch)
    ev.apply()
    assert (root / REC / halt_tree_evict.EVICTED_FILE).exists()
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths
    assert any(p.startswith(f"{REC}/{halt_tree_evict.BLOBS_DIR}/") for p in paths)
    assert (root / "mod.py").read_bytes() == BAD


def test_test_infra_failure_halt_parks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    (root / "mod.py").write_bytes(BAD)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    paths: list = []
    withheld: dict = {}
    ev = plan_tree_eviction(
        root, REC, ["mod.py"], {"mod.py"}, ["mod.py"], withheld, paths, halt_kind="test_infra_failure",
    )
    assert ev is None and paths == [] and withheld == {}
    assert (root / "mod.py").read_bytes() == BAD
