"""`_install_bin_resolvers` must not sweep a native forwarder image it wrote
in the same run.

Every derived name gets a native door image (PM ruling 2026-08-29), and on
Windows that image's filename (`<name>.exe`) is in neither target map. The
orphan sweep and the Step 3e prune used to be told only about the warm-load
allowlist's subset of those filenames, so on 2026-09-23 a Windows install
wrote 34 images, recorded them in the native manifest, then deleted them as
orphans in the same run. `chunk-commits` is one of the 34: a real derived
name that is not in `door_eligible_entrypoints`.

The writer is faked so the test needs neither a stamped engine nor a
prebuilt door. On POSIX the image filename is the bare name, which is
protected anyway, so this only fails on Windows, the platform with the defect.
"""
from __future__ import annotations

import sys
from pathlib import Path

from coordinator_core.install import door_install, substrate
from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _derive_agent_helper_target_map,
    _install_bin_resolvers,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NAME = "chunk-commits"


def test_a_native_image_outside_the_allowlist_survives_its_own_install(tmp_path, monkeypatch):
    assert _NAME in _derive_agent_helper_target_map(_REPO_ROOT / "coordinator" / "bin"), (
        f"{_NAME} is no longer a derived CLI -- pick another name outside door_eligible_entrypoints"
    )
    ml_bin, ch_bin, bin_dst = tmp_path / "ml_bin", tmp_path / "ch_bin", tmp_path / "bin_dst"
    ml_bin.mkdir()
    ch_bin.mkdir()
    bin_dst.mkdir()
    for entry in _load_bin_templates_manifest(_resolve_bin_templates_manifest_root()).install_bin_resolvers_entries():
        (ml_bin / entry.name).write_text(f"ml::{entry.name}\n", encoding="utf-8")
    for f, _exec_bit in _CH_FAMILY_FILES:
        (ch_bin / f).write_text(f"ch::{f}\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_REPO_ROOT))

    image = door_install.named_forwarder_path(bin_dst, _NAME)

    def fake_writer(target_map, dst, check_only, **_kwargs):
        image.write_bytes(b"MZ\x90\x00fake-door-image")
        substrate._write_native_forwarder_manifest(dst, {_NAME})
        return [substrate.WriteSurfaceEntry(kind="file-path", path=str(image))]

    monkeypatch.setattr(substrate, "_write_agent_helper_forwarders", fake_writer)
    monkeypatch.setattr(substrate, "_install_live_source_tree_forwarders", lambda *a, **k: frozenset())
    # The suite-wide mutation opt-out refuses every sweep delete, which would
    # pass this test whether or not the image is protected. Every path here
    # is under tmp_path.
    monkeypatch.setattr(substrate, "_refuse_machine_mutation", lambda *a, **k: None)

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst, check_only=False, python3_cmd_resolved_bin=sys.executable,
    )

    assert image.is_file(), f"{image.name} was written and then swept by the same install run"
