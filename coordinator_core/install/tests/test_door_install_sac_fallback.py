"""install_door keeps the Python shim as the bare name when the forwarder route is
`py` or the launch probe reports a Smart App Control block (injected: no test
depends on SAC being on)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.install import door_install, substrate
from coordinator_core.install.door_launch_probe import SAC_DIAGNOSIS, probe_launch

pytestmark = [
    pytest.mark.complete,
    pytest.mark.skipif(sys.platform != "win32", reason="the forwarder route is a Windows install branch"),
]

BARE = door_install.BARE_FORWARDER_NAME


def _stamped_root(tmp_path: Path) -> Path:
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")
    return root


@pytest.fixture
def bin_dst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bin dir with the Python shim pair, drift leg neutralised."""
    dst = tmp_path / "bin"
    dst.mkdir()
    (dst / BARE).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (dst / f"{BARE}.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(door_install, "committed_prebuilt_source_drift", lambda: [])
    return dst


def _install(bin_dst: Path, tmp_path: Path, **kwargs) -> Path:
    return door_install.install_door(bin_dst, _stamped_root(tmp_path), **kwargs)


def _pathext_resolve(bin_dst: Path) -> Path:
    for suffix in (".exe", ".cmd", ""):
        candidate = bin_dst / f"{BARE}{suffix}"
        if candidate.exists():
            return candidate
    raise AssertionError("bare name resolves to nothing")


def _plant_stale_door(bin_dst: Path) -> None:
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"MZ-stale")
    door_install.installed_provenance_path(bin_dst).write_text("{}", encoding="utf-8")
    (bin_dst / door_install.door_build.SIDECAR_FILENAME).write_text("root\n", encoding="utf-8")


def _blocked_probe(calls: list):
    def probe(image: Path):
        calls.append(image)
        return False, f"{SAC_DIAGNOSIS}: {OSError(4551, 'blocked')!r}"

    return probe


def test_py_route_places_no_exe_and_spawns_no_probe(bin_dst, tmp_path, capsys):
    calls: list = []
    _plant_stale_door(bin_dst)

    with pytest.raises(door_install.DoorRouteDeclined):
        _install(bin_dst, tmp_path, forwarder_route="py", launch_probe=_blocked_probe(calls))

    assert calls == []
    assert not (bin_dst / door_install.DOOR_INSTALLED_NAME).exists()
    assert not door_install.installed_provenance_path(bin_dst).exists()
    assert _pathext_resolve(bin_dst) == bin_dst / f"{BARE}.cmd"
    assert (bin_dst / BARE).exists()
    assert SAC_DIAGNOSIS not in capsys.readouterr().err


def test_exe_route_with_sac_block_keeps_the_shim_and_prints_the_diagnosis(bin_dst, tmp_path, capsys):
    calls: list = []
    _plant_stale_door(bin_dst)

    with pytest.raises(door_install.DoorRouteDeclined):
        _install(bin_dst, tmp_path, forwarder_route="exe", launch_probe=_blocked_probe(calls))

    assert len(calls) == 1
    assert not (bin_dst / door_install.DOOR_INSTALLED_NAME).exists()
    assert not (bin_dst / door_install.door_build.SIDECAR_FILENAME).exists()
    assert _pathext_resolve(bin_dst) == bin_dst / f"{BARE}.cmd"
    assert SAC_DIAGNOSIS in capsys.readouterr().err


def test_sac_block_is_recorded_in_the_degrade_sink(bin_dst, tmp_path, monkeypatch):
    recorded: list = []
    monkeypatch.setattr(door_install, "record_install_door_degrade", lambda name, exc, *a: recorded.append((name, str(exc))))

    with pytest.raises(door_install.DoorRouteDeclined):
        _install(bin_dst, tmp_path, forwarder_route="exe", launch_probe=_blocked_probe([]))

    assert [name for name, _ in recorded] == [BARE]
    assert "Smart App Control" in recorded[0][1]


def test_unset_route_is_probed_like_exe(bin_dst, tmp_path, monkeypatch):
    monkeypatch.setattr(door_install, "read_forwarder_route", lambda: None)
    calls: list = []

    with pytest.raises(door_install.DoorRouteDeclined):
        _install(bin_dst, tmp_path, launch_probe=_blocked_probe(calls))

    assert len(calls) == 1


def test_a_launching_image_is_still_placed(bin_dst, tmp_path):
    """The other side of the decision: a probe that sees the image start places it."""
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door in this checkout")
    calls: list = []

    def launching(image: Path):
        calls.append(image)
        return True, "launched (exit 2)"

    dest = _install(bin_dst, tmp_path, forwarder_route="exe", launch_probe=launching)

    assert dest.exists()
    assert len(calls) == 1


def test_a_non_sac_probe_failure_does_not_decline_the_door(bin_dst, tmp_path):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door in this checkout")

    dest = _install(bin_dst, tmp_path, forwarder_route="exe", launch_probe=lambda image: (False, "other: exit 1"))

    assert dest.exists()


def test_real_probe_classes_an_injected_winerror_4551_as_a_sac_block(bin_dst, tmp_path):
    def runner(argv, timeout):
        raise OSError(4551, "An Application Control policy has blocked this file", None, 4551)

    with pytest.raises(door_install.DoorRouteDeclined):
        _install(bin_dst, tmp_path, forwarder_route="exe", launch_probe=lambda image: probe_launch(image, runner=runner))


def test_py_route_re_emits_a_missing_shim_pair(tmp_path, monkeypatch):
    bare_dst = tmp_path / "bin"
    bare_dst.mkdir()
    monkeypatch.setattr(door_install, "committed_prebuilt_source_drift", lambda: [])

    with pytest.raises(door_install.DoorRouteDeclined):
        _install(bare_dst, tmp_path, forwarder_route="py")

    assert (bare_dst / f"{BARE}.cmd").exists()
    assert _pathext_resolve(bare_dst).name == f"{BARE}.cmd"


def test_write_native_door_forwarder_stays_on_the_python_path_when_declined(bin_dst, tmp_path, capsys, monkeypatch):
    degrades: list = []
    monkeypatch.setattr(door_install, "record_install_door_degrade", lambda *a: degrades.append(a))
    monkeypatch.setattr(door_install, "read_forwarder_route", lambda: "py")
    monkeypatch.setattr(door_install, "launcher_is_installable", lambda root, name: True)
    monkeypatch.setattr(door_install, "name_gets_door_image", lambda name: True)

    result = substrate._write_native_door_forwarder("some-tool", bin_dst, False, engine_root=_stamped_root(tmp_path))

    assert result is None
    assert not (bin_dst / "some-tool.exe").exists()
    assert degrades == []
    assert "door build failed" not in capsys.readouterr().err


def test_retire_native_images_removes_only_the_named_exes(tmp_path):
    for name in ("a.exe", "b.exe", "a.cmd"):
        (tmp_path / name).write_bytes(b"x")

    gone = door_install.retire_native_images(tmp_path, ["a", "missing"])

    assert gone == [tmp_path / "a.exe"]
    assert (tmp_path / "b.exe").exists() and (tmp_path / "a.cmd").exists()


def test_read_forwarder_route_reads_the_machine_local_key(monkeypatch):
    from coordinator_core import machine_local_spawn

    seen: list = []
    monkeypatch.setattr(machine_local_spawn, "get", lambda impl, key: (seen.append(key), (" PY ", True))[1])

    assert door_install.read_forwarder_route() == "py"
    assert seen == [door_install.FORWARDER_ROUTE_KEY]
    monkeypatch.setattr(machine_local_spawn, "get", lambda impl, key: (None, True))
    assert door_install.read_forwarder_route() is None
