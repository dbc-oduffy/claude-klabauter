"""A record path ``plan_tree_eviction`` appended but ``apply`` never wrote is dropped from ``paths``."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict, live_write_guard
from coordinator_core.ops.dispatch_emit.halted_record_eviction import plan_tree_eviction

REC = "state/warp/run-x"
BAD = b"broken\n"


def _plan(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    (root / "mod.py").write_bytes(BAD)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {p: "x" for p in own})
    _stage_run(root, "r1", {"mod.py": None})
    paths: list = ["other.txt"]
    ev = plan_tree_eviction(
        root, REC, ["mod.py"], {"mod.py"}, ["mod.py"], {}, paths, run_id="r1",
    )
    return root, ev, paths


def _stage_run(root, run_id, baseline):
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    shas = {
        p: None if b is None else git_objects.write_object(resolve_git_common_dir(root), b"blob", b)
        for p, b in baseline.items()
    }
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": shas}), encoding="utf-8"
    )


def test_every_target_refused_drops_unwritten_record_paths(tmp_path, monkeypatch):
    root, ev, paths = _plan(tmp_path, monkeypatch)
    assert ev is not None
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths
    monkeypatch.setattr(live_write_guard, "check_live_write", lambda *a, **k: "peer holds it")
    ev.apply()
    assert paths == ["other.txt"]
    assert ev.refused == {"mod.py": "peer holds it"}
    assert not (root / REC / halt_tree_evict.EVICTED_FILE).exists()


def test_written_record_paths_stay(tmp_path, monkeypatch):
    root, ev, paths = _plan(tmp_path, monkeypatch)
    monkeypatch.setattr(live_write_guard, "check_live_write", lambda *a, **k: None)
    ev.apply()
    assert (root / REC / halt_tree_evict.EVICTED_FILE).exists()
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths
    assert any(p.startswith(f"{REC}/{halt_tree_evict.BLOBS_DIR}/") for p in paths)
