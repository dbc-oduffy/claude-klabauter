"""The box-wide door sidecar (`<settings-home>/bin/door.engine-root.txt`) may only name the
registered installed engine (`repos.claude_klabauter`); a scratch build points its own
processes at itself with COORDINATOR_DOOR_ENGINE_ROOT.

Incident 2026-11-20 04:06: an install run from scratch/klabauter-1a-d25e8e9a wrote that root
into the box sidecar and every door-routed op ran the scratch engine for ~8h."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core._settings_home import settings_home
from coordinator_core.install import door_install
from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.door.sidecar_guard import OVERRIDE_ENV, DoorSidecarRefused


def _register(installed: Path) -> Path:
    ml = settings_home() / "machine-local"
    ml.mkdir(parents=True, exist_ok=True)
    (ml / "registry.local.toml").write_text(
        f"schema = 1\n[repos]\nclaude_klabauter = '{installed.as_posix()}'\n", encoding="utf-8"
    )
    return installed


@pytest.fixture
def box_bin() -> Path:
    b = settings_home() / "bin"
    b.mkdir(parents=True, exist_ok=True)
    return b


def test_scratch_root_is_refused_and_sidecar_untouched(tmp_path: Path, box_bin: Path) -> None:
    installed = _register(tmp_path / "installed")
    installed.mkdir()
    scratch = tmp_path / "repo" / "scratch" / "klabauter-1a-d25e8e9a"
    scratch.mkdir(parents=True)
    with pytest.raises(DoorSidecarRefused) as exc:
        door_build.write_sidecar(box_bin / "door.exe", scratch)
    assert str(scratch) in str(exc.value) and OVERRIDE_ENV in str(exc.value)
    assert not (box_bin / door_build.SIDECAR_FILENAME).exists()


def test_unregistered_non_scratch_root_is_refused(tmp_path: Path, box_bin: Path) -> None:
    installed = _register(tmp_path / "installed")
    installed.mkdir()
    other = tmp_path / "other-engine"
    other.mkdir()
    with pytest.raises(DoorSidecarRefused):
        door_build.write_sidecar(box_bin / "door.exe", other)


def test_registered_engine_is_accepted(tmp_path: Path, box_bin: Path) -> None:
    installed = _register(tmp_path / "installed")
    installed.mkdir()
    door_build.write_sidecar(box_bin / "door.exe", installed)
    assert (box_bin / door_build.SIDECAR_FILENAME).read_text().strip() == str(installed.resolve())


def test_a_scratch_builds_own_door_dir_is_not_guarded(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch" / "build"
    scratch.mkdir(parents=True)
    door_build.write_sidecar(scratch / "door.exe", scratch)
    assert (scratch / door_build.SIDECAR_FILENAME).exists()


def test_scratch_root_is_refused_even_when_registry_is_unset(tmp_path: Path, box_bin: Path) -> None:
    scratch = tmp_path / "scratch" / "x"
    scratch.mkdir(parents=True)
    with pytest.raises(DoorSidecarRefused):
        door_build.write_sidecar(box_bin / "door.exe", scratch)


def test_override_writes_and_logs_durably(tmp_path: Path, box_bin: Path, monkeypatch) -> None:
    installed = _register(tmp_path / "installed")
    installed.mkdir()
    scratch = tmp_path / "scratch" / "x"
    scratch.mkdir(parents=True)
    monkeypatch.setenv(OVERRIDE_ENV, "1")
    door_build.write_sidecar(box_bin / "door.exe", scratch)
    assert (box_bin / door_build.SIDECAR_FILENAME).read_text().strip() == str(scratch.resolve())
    log = settings_home() / "door-sidecar-guard.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["event"] == "override" and rows[-1]["target"] == str(scratch.resolve())
    assert rows[-1]["registered"] == installed.as_posix()


def test_install_door_refuses_before_touching_the_box_bin(tmp_path: Path, box_bin: Path, monkeypatch) -> None:
    installed = _register(tmp_path / "installed")
    installed.mkdir()
    scratch = tmp_path / "scratch" / "x"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(door_install, "is_engine_root", lambda p: True)
    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(box_bin, scratch)
    assert list(box_bin.iterdir()) == []
