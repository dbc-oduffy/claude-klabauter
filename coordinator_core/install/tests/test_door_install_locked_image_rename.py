"""Tests for `door_install._replace_possibly_running_image`'s rename-aside
path: a `PermissionError` on the direct copy (a mapped/running image on
Windows) must be recovered by renaming the destination aside and copying
into the freed path, with a full rollback if the second copy also fails.

Spec backlink: break-class fix, 2026-08-30 live `install-substrate` failure
(`PermissionError: [WinError 32] ... being used by another process` on
`shutil.copy2(_PREBUILT_DOOR_EXE, dest_exe)`).
"""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.install import door_install


def test_replace_possibly_running_image_renames_locked_dest_and_lands_new_content(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"new-image")
    dest = tmp_path / "dest.bin"
    dest.write_bytes(b"old-image")

    calls = {"n": 0}
    real_copy2 = shutil.copy2

    def flaky_copy2(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("[WinError 32] locked")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch_target = door_install.shutil
    original = monkeypatch_target.copy2
    monkeypatch_target.copy2 = flaky_copy2
    try:
        door_install._replace_possibly_running_image(source, dest)
    finally:
        monkeypatch_target.copy2 = original

    assert dest.read_bytes() == b"new-image"

    # Two copy2 attempts is what proves the rename branch ran rather than a
    # plain overwrite silently succeeding -- the first raises the lock, the
    # second lands in the path the rename freed.
    assert calls["n"] == 2

    # NO residue is the correct outcome HERE, and asserting one was the bug in
    # this test's first draft. The displaced file is only undeletable while a
    # real process still executes the old image; a simulated lock holds no
    # handle, so the helper's best-effort unlink succeeds and cleans up. A
    # surviving `.stale-` sibling is the live-box outcome, not the test one.
    assert list(tmp_path.glob("dest.bin.stale-*")) == []


def test_replace_possibly_running_image_rolls_back_when_second_copy_also_fails(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"new-image")
    dest = tmp_path / "dest.bin"
    dest.write_bytes(b"old-image")

    calls = {"n": 0}

    def always_locked_copy2(src, dst, *args, **kwargs):
        calls["n"] += 1
        raise PermissionError("[WinError 32] locked")

    monkeypatch_target = door_install.shutil
    original = monkeypatch_target.copy2
    monkeypatch_target.copy2 = always_locked_copy2
    try:
        with pytest.raises(PermissionError):
            door_install._replace_possibly_running_image(source, dest)
    finally:
        monkeypatch_target.copy2 = original

    assert dest.exists()
    assert dest.read_bytes() == b"old-image"
    assert not list(tmp_path.glob("dest.bin.stale-*"))


def test_replace_possibly_running_image_reraises_permission_error_when_dest_absent(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"new-image")
    dest = tmp_path / "dest.bin"

    def always_locked_copy2(src, dst, *args, **kwargs):
        raise PermissionError("[WinError 32] locked")

    monkeypatch_target = door_install.shutil
    original = monkeypatch_target.copy2
    monkeypatch_target.copy2 = always_locked_copy2
    try:
        with pytest.raises(PermissionError):
            door_install._replace_possibly_running_image(source, dest)
    finally:
        monkeypatch_target.copy2 = original
