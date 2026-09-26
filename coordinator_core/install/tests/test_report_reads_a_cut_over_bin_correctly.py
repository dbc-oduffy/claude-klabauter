
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.install import door_install, settings_home_report
from coordinator_core.warm.door import build as door_build

_MACH_O = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def _write_manifest(bin_dir: Path, names: list[str]) -> None:
    (bin_dir / "_native-forwarder-manifest.json").write_text(
        json.dumps({"names": names}), encoding="utf-8"
    )


def test_a_process_replacing_name_is_never_audited_for_currency(bin_dir):
    names = settings_home_report._names_the_installer_gives_an_image(
        ["blocked", "claude-doe"], bin_dir
    )
    assert names == ["blocked"]


def test_a_name_the_engine_carries_no_script_for_is_not_audited(bin_dir, tmp_path):
    engine = tmp_path / "engine"
    (engine / "coordinator" / "bin").mkdir(parents=True)
    (engine / "coordinator" / "bin" / "blocked.py").write_text("x", encoding="utf-8")
    (bin_dir / door_build.SIDECAR_FILENAME).write_text(str(engine), encoding="utf-8")

    names = settings_home_report._names_the_installer_gives_an_image(
        ["blocked", "publish"], bin_dir
    )

    assert "blocked" in names
    assert "publish" not in names


def test_an_unreadable_sidecar_drops_only_the_engine_leg(bin_dir):
    names = settings_home_report._names_the_installer_gives_an_image(
        ["blocked", "publish", "claude-doe"], bin_dir
    )
    assert names == ["blocked", "publish"]


def test_a_manifested_native_image_reads_as_door_owned(bin_dir, monkeypatch):
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    path = bin_dir / "blocked"
    path.write_bytes(_MACH_O)
    _write_manifest(bin_dir, ["blocked"])

    assert settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_a_manifested_name_whose_image_became_garbage_still_fails(bin_dir, monkeypatch):
    """Manifest membership alone would exempt a name whose image was later
    replaced by something that is not an image -- exactly the corruption the
    original `installed_name == BARE_FORWARDER_NAME` gate existed to keep
    catching."""
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    path = bin_dir / "blocked"
    path.write_text("garbage, not a real forwarder body", encoding="utf-8")
    _write_manifest(bin_dir, ["blocked"])

    assert not settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_an_unmanifested_binary_is_not_exempted(bin_dir, monkeypatch):
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    path = bin_dir / "blocked"
    path.write_bytes(_MACH_O)
    _write_manifest(bin_dir, ["something-else"])

    assert not settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_nothing_is_door_owned_when_no_door_is_installed(bin_dir, monkeypatch):
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: False)
    path = bin_dir / "blocked"
    path.write_bytes(_MACH_O)
    _write_manifest(bin_dir, ["blocked"])

    assert not settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_is_native_image_is_false_for_an_absent_path(tmp_path):
    assert not door_install.is_native_image(tmp_path / "nothing-here")
