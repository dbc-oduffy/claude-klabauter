"""sidecar.write: contained atomic write into a workflow sidecar dir, refusals, registration."""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops import sidecar_write as sw
from coordinator_core.ops._registry_map import OP_MODULE_MAP

DIR = ".coordinator-local/workflow-sidecars/w/r/l/"


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sidecar.write spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _call(tmp_path, **params):
    return sw._handler(params, repo_root=str(tmp_path))


def _files(tmp_path):
    return sorted(p for p in tmp_path.rglob("*") if p.is_file())


def test_write_lands_file_in_sidecar_dir(tmp_path):
    out = _call(tmp_path, dir=DIR, name="notes.md", content="x")
    assert out == {"path": f"{DIR}notes.md", "bytes": 1}
    assert (tmp_path / DIR / "notes.md").read_text() == "x"
    assert [p.name for p in _files(tmp_path)] == ["notes.md"]


def test_overwrite_replaces_content_and_leaves_no_temp(tmp_path):
    _call(tmp_path, dir=DIR, name="n.md", content="first")
    out = _call(tmp_path, dir=DIR, name="n.md", content="second\r\nline")
    assert "error" not in out
    assert (tmp_path / DIR / "n.md").read_bytes() == b"second\r\nline"
    assert [p.name for p in _files(tmp_path)] == ["n.md"]


@pytest.mark.parametrize(
    "dir_",
    [
        "scratch/x/",
        ".coordinator-local/other/x/",
        ".coordinator-local/workflow-sidecars",
        ".coordinator-local/workflow-sidecars/",
        ".coordinator-local/workflow-sidecars/../../escape/",
        ".coordinator-local/workflow-sidecars/w/CON/",
        "/abs/.coordinator-local/workflow-sidecars/w/",
        None,
    ],
)
def test_dir_outside_root_refused_and_writes_nothing(tmp_path, dir_):
    out = _call(tmp_path, dir=dir_, name="a.md", content="x")
    assert "error" in out
    assert _files(tmp_path) == []


@pytest.mark.parametrize("name", ["a/b.md", "a\\b.md", "..", ".", "", "CON", "nul.md", "a.", "x y.md", None])
def test_bad_name_refused_and_writes_nothing(tmp_path, name):
    out = _call(tmp_path, dir=DIR, name=name, content="x")
    assert "error" in out
    assert _files(tmp_path) == []


def test_non_string_content_refused(tmp_path):
    assert "error" in _call(tmp_path, dir=DIR, name="a.md", content=5)
    assert _files(tmp_path) == []


def test_dispatcher_root_wins_over_param_repo_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    out = sw._handler(
        {"dir": DIR, "name": "a.md", "content": "x", "repo_root": str(tmp_path)}, repo_root=str(other)
    )
    assert "error" not in out
    assert (other / DIR / "a.md").is_file()
    assert not (tmp_path / ".coordinator-local").exists()


def test_param_repo_root_used_when_no_dispatcher_root(tmp_path):
    out = sw._handler({"dir": DIR, "name": "a.md", "content": "x", "repo_root": str(tmp_path)})
    assert "error" not in out
    assert (tmp_path / DIR / "a.md").is_file()


def test_failed_replace_leaves_no_temp_file(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(sw.os, "replace", boom)
    out = _call(tmp_path, dir=DIR, name="a.md", content="x")
    assert "error" in out
    assert _files(tmp_path) == []


def test_symlinked_dir_escaping_root_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "repo"
    (root / ".coordinator-local/workflow-sidecars").mkdir(parents=True)
    try:
        (root / ".coordinator-local/workflow-sidecars/w").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted")
    out = sw._handler({"dir": ".coordinator-local/workflow-sidecars/w/", "name": "a.md", "content": "x"}, repo_root=str(root))
    assert "error" in out
    assert list(outside.iterdir()) == []


def test_registered_in_module_map():
    assert OP_MODULE_MAP[sw.OP_KEY] == "coordinator_core.ops.sidecar_write"
