"""scratch.delete: admission predicate, rename-to-tombstone, zero spawns, idempotence."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import scratch_delete as sd

RUN = "ask-test-1"
CHUNK = "X1"


def _index(version: int = 2, tracked: tuple[str, ...] = ()) -> bytes:
    body = b"".join(t.encode() + b"\0" for t in tracked)
    return b"DIRC" + version.to_bytes(4, "big") + len(tracked).to_bytes(4, "big") + body


def _repo(tmp_path: Path, writes, *, ignored=True, index=None, run_dir="state/warp") -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(index if index is not None else _index())
    (tmp_path / ".gitignore").write_text("scratch/\n" if ignored else "other/\n")
    rd = tmp_path / run_dir / RUN
    rd.mkdir(parents=True)
    manifest = {
        "run_dir": str(rd),
        "rows": [{
            "id": CHUNK, "agent_type": "a", "model": "m", "brief_path": "b.md",
            "writes": list(writes), "wave": 0,
        }],
        "review_declared_paths": [],
        "marker_path": "m",
    }
    (rd / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def _tree(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    (d / "f.txt").write_text("x")
    return d


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("scratch.delete spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_declared_path_is_moved_to_tombstone(tmp_path):
    root = _repo(tmp_path, ["scratch/base"])
    _tree(root, "scratch/base")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])
    assert out == {"deleted": ["scratch/base"], "absent": [], "refused": []}
    assert not (root / "scratch/base").exists()
    kids = list((root / "scratch" / sd.TOMBSTONE_DIRNAME).iterdir())
    assert len(kids) == 1 and kids[0].name.startswith("base-")
    assert (kids[0] / "f.txt").read_text() == "x"


def test_in_flight_run_record_under_scratch_warp_is_read(tmp_path):
    root = _repo(tmp_path, ["scratch/base"], run_dir="scratch/warp")
    _tree(root, "scratch/base")
    assert sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])["deleted"] == ["scratch/base"]


def test_absent_path_is_idempotent(tmp_path):
    root = _repo(tmp_path, ["scratch/gone"])
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/gone"])
    assert out == {"deleted": [], "absent": ["scratch/gone"], "refused": []}
    assert sd.delete_declared(root, RUN, CHUNK, ["scratch/gone"]) == out


@pytest.mark.parametrize("path,reason", [
    ("scratch/other", "not in the chunk's declared writes"),
    ("src/a", "outside scratch/"),
    ("scratch", "scratch/ itself"),
    ("scratch/warp/x", "run records"),
    ("scratch/.deleted/x", "tombstones"),
    ("/abs/scratch/a", "absolute"),
    ("scratch/../src", "'..'"),
])
def test_static_refusals_leave_disk_untouched(tmp_path, path, reason):
    declared = [] if path == "scratch/other" else [path]
    root = _repo(tmp_path, [*declared, "src/a"])
    (root / "scratch/other").mkdir(parents=True)
    (root / "scratch/warp/x").mkdir(parents=True)
    (root / "scratch/.deleted/x").mkdir(parents=True)
    (root / "src/a").mkdir(parents=True)
    out = sd.delete_declared(root, RUN, CHUNK, [path])
    assert out["deleted"] == [] and out["absent"] == []
    assert len(out["refused"]) == 1 and reason in out["refused"][0]["reason"]
    for kept in ("scratch/other", "scratch/warp/x", "scratch/.deleted/x", "src/a"):
        assert (root / kept).exists()


def test_undeclared_path_refused_even_with_scratch_prefix(tmp_path):
    root = _repo(tmp_path, ["scratch/a"])
    _tree(root, "scratch/b")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/b"])
    assert out["deleted"] == [] and (root / "scratch/b").exists()


def test_unknown_chunk_refuses_everything(tmp_path):
    root = _repo(tmp_path, ["scratch/a"])
    _tree(root, "scratch/a")
    out = sd.delete_declared(root, RUN, "NOPE", ["scratch/a"])
    assert out["deleted"] == [] and (root / "scratch/a").exists()
    assert "not a row" in out["refused"][0]["reason"]


def test_missing_manifest_refuses(tmp_path):
    (tmp_path / ".git").mkdir()
    out = sd.delete_declared(tmp_path, "ask-none", CHUNK, ["scratch/a"])
    assert out["deleted"] == [] and "manifest" in out["refused"][0]["reason"]


def test_link_component_is_refused(tmp_path):
    root = _repo(tmp_path, ["scratch/link/inner", "scratch/lnk"])
    real = _tree(root, "real")
    (root / "scratch").mkdir(exist_ok=True)
    try:
        os.symlink(real, root / "scratch/link", target_is_directory=True)
        os.symlink(real, root / "scratch/lnk", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/link/inner", "scratch/lnk"])
    assert out["deleted"] == []
    assert all("link" in r["reason"] for r in out["refused"]) and len(out["refused"]) == 2
    assert (real / "f.txt").exists() and os.path.islink(root / "scratch/lnk")


def test_tracked_file_under_path_is_refused(tmp_path):
    root = _repo(tmp_path, ["scratch/base"], index=_index(2, ("scratch/base/keep.txt",)))
    _tree(root, "scratch/base")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])
    assert out["deleted"] == [] and "git index" in out["refused"][0]["reason"]
    assert (root / "scratch/base/f.txt").exists()


def test_version_3_index_is_admitted(tmp_path):
    root = _repo(tmp_path, ["scratch/base"], index=_index(3))
    _tree(root, "scratch/base")
    assert sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])["deleted"] == ["scratch/base"]


def test_version_4_index_refuses_every_path(tmp_path):
    root = _repo(tmp_path, ["scratch/base"], index=_index(4))
    _tree(root, "scratch/base")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])
    assert out["deleted"] == [] and "version 4" in out["refused"][0]["reason"]
    assert (root / "scratch/base").exists()


def test_unreadable_index_refuses(tmp_path):
    root = _repo(tmp_path, ["scratch/base"])
    (root / ".git/index").unlink()
    _tree(root, "scratch/base")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])
    assert out["deleted"] == [] and "unreadable" in out["refused"][0]["reason"]


def test_scratch_not_gitignored_refuses(tmp_path):
    root = _repo(tmp_path, ["scratch/base"], ignored=False)
    _tree(root, "scratch/base")
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])
    assert out["deleted"] == [] and "not gitignored" in out["refused"][0]["reason"]
    assert (root / "scratch/base").exists()


def test_rename_failure_becomes_refusal(tmp_path, monkeypatch):
    root = _repo(tmp_path, ["scratch/base"])
    _tree(root, "scratch/base")

    def fail(*a, **k):
        raise PermissionError("handle open")

    monkeypatch.setattr(os, "rename", fail)
    out = sd.delete_declared(root, RUN, CHUNK, ["scratch/base"])
    assert out["deleted"] == [] and "handle open" in out["refused"][0]["reason"]
    assert (root / "scratch/base").exists()


def test_handler_through_op_entry(tmp_path):
    root = _repo(tmp_path, ["scratch/base"])
    _tree(root, "scratch/base")
    out = sd._handler(
        {"run_id": RUN, "chunk_id": CHUNK, "paths": ["scratch/base"]}, repo_root=root
    )
    assert out["deleted"] == ["scratch/base"] and out["refused"] == []
    assert "error" in sd._handler({"run_id": RUN, "chunk_id": CHUNK, "paths": "x"}, repo_root=root)


def test_brief_clause():
    assert sd.brief_clause(RUN, CHUNK, ["src/a.py"]) == ""
    clause = sd.brief_clause(RUN, CHUNK, ["src/a.py", "scratch/base"])
    assert "coordinator-invoke scratch.delete" in clause
    assert RUN in clause and CHUNK in clause and "scratch/base" in clause
    assert "src/a.py" not in clause
