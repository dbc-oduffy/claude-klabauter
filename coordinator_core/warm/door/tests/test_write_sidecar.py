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
