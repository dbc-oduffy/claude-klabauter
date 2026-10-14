"""Pins that the drift probe counts platform-suffixed native forwarders as installed."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.install.door_install import named_forwarder_path
from coordinator_core.plugin_health import forwarder_drift


def _write_manifest(bin_dir: Path, names: list[str]) -> None:
    (bin_dir / forwarder_drift._NATIVE_FORWARDER_MANIFEST).write_text(
        json.dumps({"names": names}), encoding="utf-8"
    )


def test_native_image_with_platform_suffix_counts_as_installed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    named_forwarder_path(bin_dir, "alpha").write_bytes(b"MZ\x00native")
    _write_manifest(bin_dir, ["alpha", "beta"])

    assert forwarder_drift._native_forwarder_names(bin_dir) == {"alpha"}


def test_manifest_name_with_no_file_stays_missing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_manifest(bin_dir, ["gone"])

    assert forwarder_drift._native_forwarder_names(bin_dir) == set()


def test_one_new_bin_entry_is_healed_without_full_setup(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.install import forwarder_self_heal as heal

    engine = tmp_path / "engine"
    agent_bin = engine / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    (agent_bin / "new-cli.py").write_text("print('x')\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)

    monkeypatch.setattr(
        "coordinator_core.engine_root.coordinator_engine_root_with_class",
        lambda: (str(engine), "test"),
    )
    monkeypatch.setattr("coordinator_core._settings_home.settings_home", lambda: home)

    derived = forwarder_drift._derive_names(agent_bin)
    assert derived, "fixture must derive at least one forwarder"
    assert not any(
        heal._installed_forwarder_present(home / "bin", n) for n in derived
    )

    heal._self_heal_forwarders_inner()

    assert derived <= forwarder_drift._installed_forwarder_names(home / "bin")
