"""run.paths_trash: declared paths anywhere in the repo are trashed; protected and tracked refuse."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import run_paths_trash as rpt
from coordinator_core.ops import scratch_delete as sd
from coordinator_core.ops.tests.test_scratch_delete import CHUNK, RUN, _index, _repo, _tree


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run.paths_trash spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_declared_build_dir_outside_scratch_is_trashed(tmp_path):
    root = _repo(tmp_path, ["web/.next", "raw_sources/image-hashes-cache"])
    _tree(root, "web/.next")
    _tree(root, "raw_sources/image-hashes-cache")
    out = rpt.trash_declared(root, RUN, CHUNK, ["web/.next", "raw_sources/image-hashes-cache"])
    assert out["deleted"] == ["web/.next", "raw_sources/image-hashes-cache"]
    assert out["refused"] == []
    assert not (root / "web/.next").exists()
    assert len(list((root / "scratch" / sd.TOMBSTONE_DIRNAME).iterdir())) == 2


def test_absent_path_is_idempotent(tmp_path):
    root = _repo(tmp_path, ["web/.next"])
    out = rpt.trash_declared(root, RUN, CHUNK, ["web/.next"])
    assert out == {"deleted": [], "absent": ["web/.next"], "refused": []}


@pytest.mark.parametrize("path,reason", [
    ("web/other", "not in the chunk's declared writes"),
    ("/abs/x", "absolute"),
    ("a/../b", "'..'"),
    (".git", ".git/"),
    (".git/hooks", ".git/"),
    ("state", "top-level state/"),
    ("scratch", "scratch/ itself"),
    ("scratch/warp/x", "run records"),
    ("scratch/.deleted/x", "tombstones"),
])
def test_refusals_leave_disk_untouched(tmp_path, path, reason):
    root = _repo(tmp_path, ["web/declared" if path == "web/other" else path])
    out = rpt.trash_declared(root, RUN, CHUNK, [path])
    assert out["deleted"] == []
    assert reason in out["refused"][0]["reason"]


def test_tracked_path_is_refused(tmp_path):
    root = _repo(tmp_path, ["web/src"], index=_index(tracked=("web/src/a.ts",)))
    _tree(root, "web/src")
    out = rpt.trash_declared(root, RUN, CHUNK, ["web/src"])
    assert out["deleted"] == []
    assert "git index" in out["refused"][0]["reason"]
    assert (root / "web/src/f.txt").exists()


def test_unknown_chunk_refuses_everything(tmp_path):
    root = _repo(tmp_path, ["web/.next"])
    _tree(root, "web/.next")
    out = rpt.trash_declared(root, RUN, "NOPE", ["web/.next"])
    assert out["deleted"] == [] and "not a row" in out["refused"][0]["reason"]
    assert (root / "web/.next").exists()


def test_handler_round_trip(tmp_path):
    root = _repo(tmp_path, ["web/.next"])
    _tree(root, "web/.next")
    out = rpt._handler({"run_id": RUN, "chunk_id": CHUNK, "paths": ["web/.next"],
                                  "repo_root": str(root)})
    assert out["deleted"] == ["web/.next"]


def test_link_component_is_refused(tmp_path):
    import os

    root = _repo(tmp_path, ["web/.next"])
    real = tmp_path / "elsewhere"
    real.mkdir()
    (root / "web").mkdir(exist_ok=True)
    try:
        os.symlink(real, root / "web" / ".next")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = rpt.trash_declared(root, RUN, CHUNK, ["web/.next"])
    assert out["deleted"] == []
    assert "link or junction" in out["refused"][0]["reason"]
    assert real.exists()
