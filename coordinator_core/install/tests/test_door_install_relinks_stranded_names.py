"""`install_door` must move every hard-linked door name onto the image it installs.

The per-name forwarders in a bin dir are hard links to `coordinator-invoke`. A
POSIX build writes the door as a NEW file, so without a re-link every other
name keeps running the replaced image while the install reports success --
measured 2026-09-22 as 443 of 444 names stranded by one `door_install --bin-dst`.

Spec backlink: state/bug-backlog/2026-09-22-door-install-on-posix-strands-every-hard-98dbac369574.yaml
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.install import door_install
from coordinator_core.install import door_install_posix_build
from coordinator_core.warm.door import build as door_build

NAMES = ("hook-run", "cross-repo-memo", "coordinator-queue-append")


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def _fake_build(*, in_place: bool, payload: bytes):
    def _build_or_advise(engine_root, *, python_bin=None, compiler=None, output=None):
        if not in_place and output.exists():
            output.unlink()  # a real compile writes a new file, i.e. a new inode
        output.write_bytes(payload)
        door_build.write_sidecar(output, engine_root)
        return door_install_posix_build.PosixDoorBuildResult(built=True, output=output, advisory=None)

    return _build_or_advise


@pytest.fixture
def linked_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # Force the build branch: the currency skip is not what these tests exercise.
    monkeypatch.setattr(
        door_install, "verify_installed_provenance", lambda bin_dst: type("V", (), {"status": "stale"})()
    )
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(b"old-door")
    for name in NAMES:
        os.link(door, bin_dst / name)
    (bin_dst / "unrelated-tool").write_bytes(b"old-door")  # same bytes, different file
    return engine_root, bin_dst


def test_a_new_image_carries_every_linked_name_with_it(linked_bin, monkeypatch, capsys):
    engine_root, bin_dst = linked_bin
    monkeypatch.setattr(
        door_install_posix_build, "build_or_advise", _fake_build(in_place=False, payload=b"new-door")
    )

    dest = door_install.install_door(bin_dst, engine_root)

    door_ino = os.stat(dest).st_ino
    for name in NAMES:
        assert os.stat(bin_dst / name).st_ino == door_ino, f"{name} stranded on the old image"
        assert (bin_dst / name).read_bytes() == b"new-door"
    assert os.stat(dest).st_nlink == len(NAMES) + 1
    assert (bin_dst / "unrelated-tool").read_bytes() == b"old-door", "a non-door file was touched"
    assert f"re-linked {len(NAMES)} name(s)" in capsys.readouterr().out
    assert not [p for p in bin_dst.iterdir() if ".relink-" in p.name], "a temp link was left behind"


def test_an_in_place_rewrite_needs_no_relink(linked_bin, monkeypatch, capsys):
    engine_root, bin_dst = linked_bin
    monkeypatch.setattr(
        door_install_posix_build, "build_or_advise", _fake_build(in_place=True, payload=b"new-door")
    )

    door_install.install_door(bin_dst, engine_root)

    for name in NAMES:
        assert (bin_dst / name).read_bytes() == b"new-door"
    assert "re-linked" not in capsys.readouterr().out


def test_a_name_left_on_the_old_image_fails_the_install(linked_bin, monkeypatch):
    engine_root, bin_dst = linked_bin
    monkeypatch.setattr(
        door_install_posix_build, "build_or_advise", _fake_build(in_place=False, payload=b"new-door")
    )
    monkeypatch.setattr(door_install, "_link_over", lambda source, dest: None)

    with pytest.raises(door_install.DoorInstallError, match="still run the replaced"):
        door_install.install_door(bin_dst, engine_root)


def test_a_fresh_install_has_nothing_to_relink(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        door_install_posix_build, "build_or_advise", _fake_build(in_place=False, payload=b"new-door")
    )
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    dest = door_install.install_door(tmp_path / "bin", engine_root)

    assert dest.read_bytes() == b"new-door"
