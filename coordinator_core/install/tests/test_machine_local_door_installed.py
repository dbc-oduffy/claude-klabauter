"""`machine-local` reaches the door: it is allowlisted, reserved to its static
family (so the generic census never derives it), and the door-only step lays a
native image for it under a fixture bin.

Fixture bin under tmp_path only, never the live bin.
"""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.install import door_install, substrate

_REPO = Path(__file__).resolve().parents[3]
_ALLOWLIST = _REPO / "coordinator_core" / "ops" / "warm_entrypoint_allowlist.json"
_NAME = "machine-local"
_IMAGE = b"MZ native door image"


def _allowlist() -> dict:
    return json.loads(_ALLOWLIST.read_text(encoding="utf-8"))


def test_both_allowlists_name_machine_local_and_not_the_install_class_helper():
    data = _allowlist()
    for key in ("entrypoints", "door_eligible_entrypoints"):
        assert _NAME in data[key], key
        assert "_machine_local" not in data[key], key


def test_machine_local_is_reserved_so_the_door_only_step_not_the_census_serves_it():
    target_map = substrate._derive_agent_helper_target_map(_REPO / "coordinator" / "bin")
    assert _NAME not in target_map
    assert _NAME in substrate._AGENT_HELPER_RESERVED_NAMES
    assert _NAME in substrate._static_family_door_only_names(
        substrate._static_bin_family_names()
    )
    assert door_install.name_gets_door_image(_NAME)
    assert door_install.declared_install_class(_NAME) is not True
    assert door_install.engine_carries_entrypoint_script(_REPO, _NAME)


def test_door_only_step_lays_a_native_machine_local_image_under_a_fixture_bin(
    monkeypatch, tmp_path
):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(_IMAGE)
    (bin_dst / _NAME).write_bytes(b"ML-SHIM")
    (bin_dst / f"{_NAME}.cmd").write_bytes(b"ML-CMD")
    monkeypatch.setattr(substrate, "_door_only_platform", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.engine_root.is_engine_root", lambda root: True)
    monkeypatch.setattr(door_install, "install_door", lambda *a, **k: door)

    written = substrate._write_static_family_door_only_images(
        bin_dst, _REPO, substrate._static_bin_family_names(), False
    )

    image = door_install.named_forwarder_path(bin_dst, _NAME)
    assert image.name in written
    assert tmp_path in image.parents
    assert door_install.is_native_image(image)
    assert image.read_bytes() == _IMAGE
    assert (bin_dst / f"{_NAME}.cmd").read_bytes() == b"ML-CMD"


def test_door_only_step_lays_nothing_when_the_engine_lacks_the_entrypoint(
    monkeypatch, tmp_path
):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    monkeypatch.setattr(substrate, "_door_only_platform", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.engine_root.is_engine_root", lambda root: True)
    monkeypatch.setattr(door_install, "engine_carries_entrypoint_script", lambda root, n: False)

    written = substrate._write_static_family_door_only_images(
        bin_dst, _REPO, substrate._static_bin_family_names(), False
    )

    assert written == frozenset()
    assert not door_install.named_forwarder_path(bin_dst, _NAME).exists()
