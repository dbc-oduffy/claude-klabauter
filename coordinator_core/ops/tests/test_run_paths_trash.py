"""run.paths_trash: declared paths anywhere in the repo are trashed; protected and tracked refuse."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import run_paths_trash as rpt
from coordinator_core.ops import scratch_delete as sd
from coordinator_core.ops.tests.test_scratch_delete import CHUNK, RUN, _index, _repo, _tree
from pathlib import Path


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("run.paths_trash spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_declared_build_dir_outside_scratch_is_trashed(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["web/.next", "raw_sources/image-hashes-cache"])
    _tree(root, "web/.next")
    _tree(root, "raw_sources/image-hashes-cache")
    out = rpt.trash_declared(root, RUN, CHUNK, ["web/.next", "raw_sources/image-hashes-cache"])
    assert out["deleted"] == ["web/.next", "raw_sources/image-hashes-cache"]
    assert out["refused"] == []
    assert not (root / "web/.next").exists()
    assert len(list((root / "scratch" / sd.TOMBSTONE_DIRNAME).iterdir())) == 2


def test_absent_path_is_idempotent(tmp_path: Path) -> None:
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
def test_refusals_leave_disk_untouched(tmp_path: Path, path, reason) -> None:
    root = _repo(tmp_path, ["web/declared" if path == "web/other" else path])
    out = rpt.trash_declared(root, RUN, CHUNK, [path])
    assert out["deleted"] == []
    assert reason in out["refused"][0]["reason"]


def test_tracked_path_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["web/src"], index=_index(tracked=("web/src/a.ts",)))
    _tree(root, "web/src")
    out = rpt.trash_declared(root, RUN, CHUNK, ["web/src"])
    assert out["deleted"] == []
    assert "git index" in out["refused"][0]["reason"]
    assert (root / "web/src/f.txt").exists()


def test_unknown_chunk_refuses_everything(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["web/.next"])
    _tree(root, "web/.next")
    out = rpt.trash_declared(root, RUN, "NOPE", ["web/.next"])
    assert out["deleted"] == [] and "not a row" in out["refused"][0]["reason"]
    assert (root / "web/.next").exists()


def test_handler_round_trip(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["web/.next"])
    _tree(root, "web/.next")
    out = rpt._handler({"run_id": RUN, "chunk_id": CHUNK, "paths": ["web/.next"],
                                  "repo_root": str(root)})
    assert out["deleted"] == ["web/.next"]


def test_runless_untracked_dir_is_trashed(tmp_path: Path) -> None:
    root = _repo(tmp_path, [])
    _tree(root, "example-game-repo/.venv")
    out = rpt._handler({"paths": ["example-game-repo/.venv"], "repo_root": str(root)})
    assert out["deleted"] == ["example-game-repo/.venv"]
    assert out["refused"] == []
    assert not (root / "example-game-repo/.venv").exists()
    assert len(list((root / "scratch" / sd.TOMBSTONE_DIRNAME).iterdir())) == 1


def test_runless_tracked_path_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, [], index=_index(tracked=("web/src/a.ts",)))
    _tree(root, "web/src")
    out = rpt._handler({"paths": ["web/src"], "repo_root": str(root)})
    assert out["deleted"] == []
    assert "git index" in out["refused"][0]["reason"]
    assert (root / "web/src/f.txt").exists()


@pytest.mark.parametrize("path,reason", [
    ("/abs/x", "absolute"),
    ("a/../b", "'..'"),
    (".git/hooks", ".git/"),
    ("state", "top-level state/"),
    ("scratch/warp/x", "run records"),
])
def test_runless_protected_paths_are_refused(tmp_path: Path, path, reason) -> None:
    root = _repo(tmp_path, [])
    out = rpt._handler({"paths": [path], "repo_root": str(root)})
    assert out["deleted"] == []
    assert reason in out["refused"][0]["reason"]


def test_runless_link_component_is_refused(tmp_path: Path) -> None:
    import os

    root = _repo(tmp_path, [])
    real = tmp_path / "elsewhere"
    real.mkdir()
    (root / "web").mkdir(exist_ok=True)
    try:
        os.symlink(real, root / "web" / ".next")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = rpt._handler({"paths": ["web/.next"], "repo_root": str(root)})
    assert out["deleted"] == []
    assert "link or junction" in out["refused"][0]["reason"]
    assert real.exists()


def test_half_a_run_pair_is_still_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path, [])
    out = rpt._handler({"run_id": RUN, "paths": ["x"], "repo_root": str(root)})
    assert "chunk_id must be" in out["error"]


def test_link_component_is_refused(tmp_path: Path) -> None:
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
