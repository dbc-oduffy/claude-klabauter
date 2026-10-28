"""kept_pin: pinned blobs survive `git gc --prune=now`; unpinned ones do not."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects, kept_pin

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def _gc(repo: Path) -> None:
    _git(repo, "gc", "--prune=now", "-q")
    git_objects._OBJECT_CACHE.clear()


def _blob(repo: Path, data: bytes) -> str:
    return git_objects.write_object(repo / ".git", b"blob", data)


def test_pinned_blobs_survive_gc_and_unreachable_is_empty(repo: Path) -> None:
    common = repo / ".git"
    a, b = _blob(repo, b"alpha\n"), _blob(repo, b"beta\n")
    assert kept_pin.pin_kept(common, "run1", [a, b]) is True
    _gc(repo)
    assert git_objects.read_object(common, a) == ("blob", b"alpha\n")
    assert git_objects.read_object(common, b) == ("blob", b"beta\n")
    assert kept_pin.unreachable_kept(common, "run1", [a, b]) == []


def test_unpinned_blob_is_pruned_and_pin_deletion_names_it(repo: Path) -> None:
    common = repo / ".git"
    a, b = _blob(repo, b"alpha\n"), _blob(repo, b"beta\n")
    assert kept_pin.pin_kept(common, "run1", [a, b])
    _git(repo, "update-ref", "-d", kept_pin.KEPT_REF_PREFIX + "run1")
    _gc(repo)
    assert kept_pin.unreachable_kept(common, "run1", [b, a]) == sorted([a, b])


def test_second_pin_unions_with_prior_and_chains_parent(repo: Path) -> None:
    common = repo / ".git"
    a, b = _blob(repo, b"alpha\n"), _blob(repo, b"beta\n")
    assert kept_pin.pin_kept(common, "run1", [a])
    first = git_objects.read_ref_loose_or_packed(common, kept_pin.KEPT_REF_PREFIX + "run1")
    assert kept_pin.pin_kept(common, "run1", [b])
    ref = kept_pin.KEPT_REF_PREFIX + "run1"
    assert _git(repo, "rev-parse", ref + "^") == first
    assert set(_git(repo, "ls-tree", "--name-only", ref).split()) == {a, b}
    _gc(repo)
    assert kept_pin.unreachable_kept(common, "run1", [a, b]) == []


def test_resolving_blob_missing_from_pin_is_pinned_not_refused(repo: Path) -> None:
    common = repo / ".git"
    a = _blob(repo, b"alpha\n")
    assert kept_pin.unreachable_kept(common, "legacy", [a]) == []
    _gc(repo)
    assert git_objects.read_object(common, a) == ("blob", b"alpha\n")


def test_empty_blobs_is_a_noop_true(repo: Path) -> None:
    common = repo / ".git"
    assert kept_pin.pin_kept(common, "run1", []) is True
    assert git_objects.read_ref_loose_or_packed(common, kept_pin.KEPT_REF_PREFIX + "run1") is None
