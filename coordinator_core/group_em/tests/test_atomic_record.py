"""Tests for `coordinator_core.group_em.atomic_record` -- the shared holder-record primitives.

Ported from DoE-claude `coordinator/tests/test_atomic_record.py` (W2-C1). Zero subprocess spawns:
every case calls the module's functions in-process against a `tmp_path`-scoped directory. This
suite pins the atomic-write path, repo-key derivation, and the OS-lock primitive in isolation.
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import atomic_record as ar


def test_write_json_atomic_creates_parent_and_writes_readable_json(tmp_path):
    target = tmp_path / "nested" / "record.json"
    ar.write_json_atomic(target, {"a": 1})
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_write_json_atomic_no_temp_file_left_behind_on_success(tmp_path):
    target = tmp_path / "record.json"
    ar.write_json_atomic(target, {"a": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_write_json_atomic_overwrites_existing_file(tmp_path):
    """The Windows-hostile case: `os.replace` over an existing destination must succeed,
    not raise `PermissionError`/`FileExistsError` as a naive `os.rename` would on Windows.
    """
    target = tmp_path / "record.json"
    ar.write_json_atomic(target, {"a": 1})
    ar.write_json_atomic(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


def test_write_json_atomic_never_exposes_a_torn_or_absent_read(tmp_path):
    """A reader polling the destination path during/around a write must always observe
    either the old complete record or the new complete record -- never a missing file and
    never a partial one. Simulated in-process (no real concurrency) by reading immediately
    before and immediately after each write.
    """
    target = tmp_path / "record.json"
    ar.write_json_atomic(target, {"v": 0})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 0}

    for i in range(1, 5):
        # Reader observes the prior complete record right up until the swap.
        pre = json.loads(target.read_text(encoding="utf-8"))
        assert pre == {"v": i - 1}
        ar.write_json_atomic(target, {"v": i})
        post = json.loads(target.read_text(encoding="utf-8"))
        assert post == {"v": i}


def test_write_json_atomic_temp_file_lands_on_same_filesystem_as_destination(tmp_path, monkeypatch):
    """`tempfile.mkstemp(dir=...)` must be called with the DESTINATION's own parent directory,
    not the platform default temp dir -- otherwise `os.replace` can raise `OSError` (cross-
    device rename) on any host where the system temp dir is a different filesystem/volume
    than the target (a real Windows case: temp on C:, destination on a mapped/network drive).
    """
    target = tmp_path / "sub" / "record.json"
    seen_dirs = []
    real_mkstemp = ar.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        seen_dirs.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(ar.tempfile, "mkstemp", spy_mkstemp)
    ar.write_json_atomic(target, {"a": 1})
    assert seen_dirs == [str(target.parent)]


def test_write_json_atomic_cleans_up_temp_file_on_write_failure(tmp_path, monkeypatch):
    target = tmp_path / "record.json"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ar.os, "replace", boom)
    try:
        ar.write_json_atomic(target, {"a": 1})
        assert False, "expected OSError to propagate"
    except OSError:
        pass
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_repo_key_deterministic_and_stable_across_calls():
    a = ar.repo_key("/some/repo/root")
    b = ar.repo_key("/some/repo/root")
    assert a == b


def test_repo_key_separator_normalised():
    """Trailing/redundant separators collapse to the same key -- `os.path.normpath` covers
    this on every platform, distinct from the deliberately-unclaimed drive-letter/UNC
    identity case (see the module docstring).
    """
    a = ar.repo_key("/some/repo/root/")
    b = ar.repo_key("/some/repo/root")
    assert a == b


def test_repo_key_digest_case_normalised_where_platform_defines_it():
    """The collision-resistant digest half of the key is derived from `os.path.normcase`
    output, so it collapses on case where the platform does (Windows) and stays distinct
    where it doesn't (POSIX). The human-readable stem PREFIX is deliberately NOT
    normcase'd -- it comes straight from `Path(repo_root).name` -- so the two spellings can
    still produce different full keys even when their digests agree; this test asserts only
    the digest half, which is the actual dedup contract.
    """
    import os as _os

    a_digest = ar.repo_key("/Some/Repo/Root").rsplit("-", 1)[-1]
    b_digest = ar.repo_key("/some/repo/root").rsplit("-", 1)[-1]
    if _os.path.normcase("/Some/Repo/Root") == _os.path.normcase("/some/repo/root"):
        assert a_digest == b_digest
    else:
        assert a_digest != b_digest


def test_repo_key_distinct_roots_with_same_basename_do_not_collide():
    a = ar.repo_key("/one/path/repo")
    b = ar.repo_key("/other/path/repo")
    assert a != b


def test_holder_lock_ignores_a_lock_file_left_on_disk(tmp_path):
    """A `.lock` file with no live holder -- a crashed process's leftover -- never blocks: the
    kernel lock died with its holder, and the file itself is inert."""
    target = tmp_path / "record.json"
    (tmp_path / "record.json.lock").write_text("12345", encoding="ascii")
    with ar.holder_lock(target, timeout=0.5):
        pass


def test_holder_lock_refuses_a_second_holder_until_release(tmp_path):
    target = tmp_path / "record.json"
    with ar.holder_lock(target):
        with pytest.raises(ar.LockTimeout):
            with ar.holder_lock(target, timeout=0.2):
                pass
    with ar.holder_lock(target, timeout=0.5):
        pass
