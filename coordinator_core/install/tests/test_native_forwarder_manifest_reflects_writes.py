from __future__ import annotations

from coordinator_core.install import substrate
from coordinator_core.install.substrate import (
    _read_native_forwarder_manifest,
    _union_native_forwarder_manifest,
    _write_native_forwarder_manifest,
)


def test_union_adds_new_name_and_keeps_existing_record(tmp_path):
    _write_native_forwarder_manifest(tmp_path, {"already-recorded-cli"})

    _union_native_forwarder_manifest(tmp_path, {"newly-healed-cli"})

    assert _read_native_forwarder_manifest(tmp_path) == {
        "already-recorded-cli",
        "newly-healed-cli",
    }


def test_union_with_empty_written_names_is_a_noop(tmp_path):
    _write_native_forwarder_manifest(tmp_path, {"already-recorded-cli"})

    _union_native_forwarder_manifest(tmp_path, set())

    assert _read_native_forwarder_manifest(tmp_path) == {"already-recorded-cli"}


def test_union_against_absent_manifest_writes_only_the_new_names(tmp_path):
    _union_native_forwarder_manifest(tmp_path, {"first-ever-native-cli"})

    assert _read_native_forwarder_manifest(tmp_path) == {"first-ever-native-cli"}


def test_self_heal_inner_unions_into_a_stale_nonempty_manifest(monkeypatch, tmp_path):
    from coordinator_core.install import forwarder_self_heal

    agent_bin = tmp_path / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    (agent_bin / "healed-cli").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    bin_dst = tmp_path / "settings-home" / "bin"
    bin_dst.mkdir(parents=True)
    _write_native_forwarder_manifest(bin_dst, {"already-recorded-cli"})

    import coordinator_core.install.substrate as substrate_mod
    import coordinator_core.engine_root as engine_root_mod
    import coordinator_core._settings_home as settings_home_mod
    import coordinator_core.warm.engine_root as warm_engine_root_mod

    monkeypatch.setattr(
        engine_root_mod, "coordinator_engine_root_with_class", lambda: (str(tmp_path), "engine")
    )
    monkeypatch.setattr(settings_home_mod, "settings_home", lambda: tmp_path / "settings-home")
    monkeypatch.setattr(forwarder_self_heal, "_installed_forwarder_present", lambda *a, **k: False)
    monkeypatch.setattr(
        substrate_mod, "_derive_agent_helper_target_map", lambda agent_bin: {"healed-cli": "healed-cli"}
    )
    monkeypatch.setattr(
        substrate_mod,
        "_cut_over_to_native_door",
        lambda name, dst, check_only, engine_root=None: (dst / f"{name}.exe"),
    )
    monkeypatch.setattr(warm_engine_root_mod, "is_engine_root", lambda root: True)

    forwarder_self_heal._self_heal_forwarders_inner()

    assert _read_native_forwarder_manifest(bin_dst) == {
        "already-recorded-cli",
        "healed-cli",
    }
