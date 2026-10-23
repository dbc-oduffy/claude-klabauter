"""Tests for `coordinator_core.git.merge_state` over merges built with real `git merge`."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import merge_state as ms

pytestmark = [pytest.mark.spawns_process]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _w(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def merging(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    for n in ("shared.txt", "auto.txt", "kept.txt", "bystander.txt"):
        _w(r, n, "base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")

    _git(r, "checkout", "-q", "-b", "other")
    _w(r, "shared.txt", "other\n")
    _w(r, "auto.txt", "other auto\n")
    _git(r, "commit", "-qam", "other side")

    _git(r, "checkout", "-q", "base")
    _w(r, "shared.txt", "mine\n")
    _w(r, "kept.txt", "mine kept\n")
    _git(r, "commit", "-qam", "my side")

    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists()
    return r


def test_no_merge_reads_none(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    assert ms.read_merge_state(r) is None


def test_read_classifies_paths(merging):
    state = ms.read_merge_state(merging)
    assert state.merge_head == _git(merging, "rev-parse", "MERGE_HEAD").stdout.strip()
    assert state.unmerged == ("shared.txt",)
    assert "shared.txt" in state.merge_paths
    assert "auto.txt" in state.merge_paths
    assert "kept.txt" not in state.merge_paths
    assert "bystander.txt" not in state.merge_paths


def test_read_spawns_nothing(merging, monkeypatch):
    calls = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    assert ms.read_merge_state(merging) is not None
    assert calls == []


def test_unreadable_tree_raises(merging):
    (merging / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    with pytest.raises(ms.MergeStateError):
        ms.read_merge_state(merging)


def test_markers_refuse_then_resolved_passes(merging):
    state = ms.read_merge_state(merging)
    paths = ["shared.txt", "auto.txt"]
    assert ms.merge_refusal(state, paths, merging).startswith(ms.MERGE_MARKERS)
    _w(merging, "shared.txt", "resolved\n")
    assert ms.merge_refusal(state, paths, merging) is None


def test_marker_in_auto_merged_path_is_not_scanned(merging):
    state = ms.read_merge_state(merging)
    _w(merging, "shared.txt", "resolved\n")
    _w(merging, "auto.txt", "<<<<<<< x\nfixture\n>>>>>>> y\n")
    assert ms.merge_refusal(state, ["shared.txt", "auto.txt"], merging) is None


def test_unresolved_path_omitted(merging):
    state = ms.read_merge_state(merging)
    _w(merging, "shared.txt", "resolved\n")
    assert ms.merge_refusal(state, ["auto.txt"], merging).startswith(ms.MERGE_UNRESOLVED)


def test_foreign_path_refused(merging):
    state = ms.read_merge_state(merging)
    _w(merging, "shared.txt", "resolved\n")
    out = ms.merge_refusal(state, ["shared.txt", "auto.txt", "bystander.txt"], merging)
    assert out.startswith(ms.MERGE_FOREIGN) and "bystander.txt" in out


def test_incomplete_pathspec_refused(merging):
    state = ms.read_merge_state(merging)
    state = state._replace(unmerged=())
    out = ms.merge_refusal(state, ["shared.txt"], merging)
    assert out.startswith(ms.MERGE_INCOMPLETE) and "auto.txt" in out


def test_octopus_refused(merging):
    state = ms.read_merge_state(merging)._replace(merge_head="a" * 40 + "\n" + "b" * 40)
    assert ms.merge_refusal(state, [], merging).startswith(ms.MERGE_OCTOPUS)


def test_clear_removes_all_four(tmp_path):
    for n in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE"):
        (tmp_path / n).write_text("x")
    (tmp_path / "keep").write_text("x")
    ms.clear_merge_state(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep"]
    ms.clear_merge_state(tmp_path)
