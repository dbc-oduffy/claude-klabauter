"""merge_landing.run_owned_merge: the owner gate and the present/deleted split."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import merge_owner, merge_state
from coordinator_core.ops.dispatch_emit.merge_landing import run_owned_merge

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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
    for n in ("shared.txt", "auto.txt", "kept.txt", "gone.txt"):
        _w(r, n, "base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    _git(r, "checkout", "-q", "-b", "other")
    _w(r, "shared.txt", "other\n")
    _w(r, "auto.txt", "other auto\n")
    _git(r, "rm", "-q", "gone.txt")
    _git(r, "commit", "-qam", "other side")
    _git(r, "checkout", "-q", "base")
    _w(r, "shared.txt", "mine\n")
    _w(r, "kept.txt", "mine kept\n")
    _git(r, "commit", "-qam", "my side")
    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists()
    _w(r, "shared.txt", "resolved\n")
    _git(r, "add", "shared.txt")
    return r


def _own(repo, run_id="run-1"):
    mh = _git(repo, "rev-parse", "MERGE_HEAD").stdout.strip()
    h = _git(repo, "rev-parse", "HEAD").stdout.strip()
    merge_owner.record(repo, run_id, mh, h)


def test_owned_merge_splits_present_and_deleted(merging):
    _own(merging)
    got = run_owned_merge(merging, "run-1")
    assert got.present == ("auto.txt", "shared.txt")
    assert got.deleted == ("gone.txt",)


def test_no_merge_head_is_none_without_reading_state(merging, monkeypatch):
    _git(merging, "merge", "--abort")

    def boom(*a, **k):
        raise AssertionError("read_merge_state called")

    monkeypatch.setattr(merge_state, "read_merge_state", boom)
    assert run_owned_merge(merging, "run-1") is None
    assert run_owned_merge(merging, None) is None


def test_other_or_no_owner_is_none(merging):
    assert run_owned_merge(merging, "run-1") is None
    _own(merging, "run-2")
    assert run_owned_merge(merging, "run-1") is None


def test_unreadable_state_fails_closed(merging, monkeypatch):
    _own(merging)

    def bad(*a, **k):
        raise merge_state.MergeStateError("nope")

    monkeypatch.setattr(merge_state, "read_merge_state", bad)
    assert run_owned_merge(merging, "run-1") is None
