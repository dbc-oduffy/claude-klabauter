"""`door_build.write_sidecar` is called once per named forwarder while live
doors read the file, so an unchanged sidecar must not be rewritten and a
changed one must land whole -- a truncating write under a concurrent reader
failed four names with `[Errno 13]` on a real Windows install."""

from __future__ import annotations

import os

import pytest

from coordinator_core.warm.door import build as door_build


def _sidecar(tmp_path):
    return tmp_path / door_build.SIDECAR_FILENAME


def test_writes_the_resolved_root_as_one_line(tmp_path):
    exe = tmp_path / "door.exe"
    door_build.write_sidecar(exe, tmp_path)
    assert _sidecar(tmp_path).read_bytes() == (str(tmp_path.resolve()) + "\n").encode("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlink_engine_root_is_recorded_literally_not_resolved(tmp_path):
    """A cloud session's `/root/engine-current`-shaped symlink must be
    recorded UNRESOLVED, so a later repoint of the link (see
    `coordinator_core.hooks.repin_cloud_engine_root`) is followed by the
    door's own runtime `realpath()` call without any sidecar rewrite --
    see `door_build._sidecar_root_string`'s docstring for the incident
    this pins."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    link = tmp_path / "engine-current"
    link.symlink_to(target_dir)
    exe = tmp_path / "door.exe"

    door_build.write_sidecar(exe, link)

    assert _sidecar(tmp_path).read_bytes() == (str(link) + "\n").encode("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_repointed_symlink_root_changes_what_a_fresh_sidecar_write_would_record(tmp_path):
    """Companion to the above: repointing the link changes the LITERAL
    string written verbatim -- proving the recorded value tracks the link
    identity, not a frozen resolution of whichever target it had at write
    time."""
    first_target = tmp_path / "first"
    first_target.mkdir()
    second_target = tmp_path / "second"
    second_target.mkdir()
    link = tmp_path / "engine-current"
    link.symlink_to(first_target)
    exe = tmp_path / "door.exe"

    door_build.write_sidecar(exe, link)
    assert _sidecar(tmp_path).read_bytes() == (str(link) + "\n").encode("utf-8")

    link.unlink()
    link.symlink_to(second_target)
    # The recorded LITERAL string is unchanged (still the link path) --
    # what changes is what the OS resolves it to, which is exactly the
    # property this write preserves.
    door_build.write_sidecar(exe, link)
    assert _sidecar(tmp_path).read_bytes() == (str(link) + "\n").encode("utf-8")
    assert os.path.realpath(link) == str(second_target.resolve())


def test_an_unchanged_sidecar_is_not_rewritten(tmp_path, monkeypatch):
    exe = tmp_path / "door.exe"
    door_build.write_sidecar(exe, tmp_path)

    def no_replace(*_a, **_k):
        raise AssertionError("an unchanged sidecar was rewritten")

    monkeypatch.setattr(door_build.os, "replace", no_replace)
    door_build.write_sidecar(exe, tmp_path)


def test_a_transient_sharing_violation_is_retried(tmp_path, monkeypatch):
    exe = tmp_path / "door.exe"
    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(13, "Permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(door_build.os, "replace", flaky_replace)
    monkeypatch.setattr(door_build.time, "sleep", lambda _s: None)
    door_build.write_sidecar(exe, tmp_path)

    assert len(calls) == 3
    assert _sidecar(tmp_path).read_bytes() == (str(tmp_path.resolve()) + "\n").encode("utf-8")


def test_a_persistent_hold_raises_and_leaves_no_temp(tmp_path, monkeypatch):
    exe = tmp_path / "door.exe"

    def held(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(door_build.os, "replace", held)
    monkeypatch.setattr(door_build.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        door_build.write_sidecar(exe, tmp_path)

    assert [p.name for p in tmp_path.iterdir()] == []


def test_a_non_sharing_failure_raises_at_once_and_leaves_no_temp(tmp_path, monkeypatch):
    exe = tmp_path / "door.exe"
    calls = []

    def gone(*_a, **_k):
        calls.append(1)
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(door_build.os, "replace", gone)
    with pytest.raises(FileNotFoundError):
        door_build.write_sidecar(exe, tmp_path)

    assert calls == [1]
    assert [p.name for p in tmp_path.iterdir()] == []
