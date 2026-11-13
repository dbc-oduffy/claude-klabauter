"""The scratch-.py deny is judged below the enclosing repo root, consistent with the write budget's exemption."""

from __future__ import annotations

from coordinator_core.hooks import nudge_em_code_dispatch as hook


def _repo(base):
    repo = base / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_nested_git_probe_repo_under_scratch_is_not_scratch_py(tmp_path):
    repo = _repo(tmp_path / "scratch" / "fam-1a")
    target = str(repo / "src" / "x.py")
    assert not hook._is_scratch_py(target)


def test_repo_scratch_py_is_denied_shape(tmp_path):
    repo = _repo(tmp_path / "scratch" / "fam-1a")
    assert hook._is_scratch_py(str(repo / "scratch" / "probe.py"))
    assert hook._is_scratch_py(str(repo / "src" / "scratch" / "probe.py"))


def test_no_repo_falls_back_to_absolute_path(tmp_path):
    assert hook._is_scratch_py(str(tmp_path / "scratch" / "probe.py"))
    assert not hook._is_scratch_py(str(tmp_path / "other" / "probe.py"))


def test_non_py_is_not_scratch_py(tmp_path):
    repo = _repo(tmp_path)
    assert not hook._is_scratch_py(str(repo / "scratch" / "probe.txt"))
