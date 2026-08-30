"""Tests for coordinator_core.install.door_install's platform-resolved
naming: `DOOR_INSTALLED_NAME` / `_PREBUILT_DOOR_EXE` / `_PREBUILT_PROVENANCE`
must match the current platform's own build module's output convention
(`build.py`'s `door.exe` on Windows, `build_posix.py`'s extensionless
`door` everywhere else), and `is_door_installed()` must answer presence
without raising.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C3.md
"""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from coordinator_core.install import door_install
from coordinator_core.warm.door import build as door_build


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def test_door_installed_name_is_platform_resolved():
    if sys.platform == "win32":
        assert door_install.DOOR_INSTALLED_NAME == "coordinator-invoke.exe"
    else:
        assert door_install.DOOR_INSTALLED_NAME == "coordinator-invoke"


def test_prebuilt_lookup_and_provenance_match_platform_build_convention():
    if sys.platform == "win32":
        assert door_install._PREBUILT_DOOR_EXE.name == "door.exe"
        assert door_install._PREBUILT_PROVENANCE.name == "door.exe.provenance.json"
    else:
        assert door_install._PREBUILT_DOOR_EXE.name == "door"
        assert door_install._PREBUILT_PROVENANCE.name == "door.provenance.json"
    assert door_install._PREBUILT_DOOR_EXE.parent == door_install._PREBUILT_PROVENANCE.parent


def test_is_door_installed_false_on_absence(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    assert door_install.is_door_installed(bin_dst) is False


def test_is_door_installed_true_after_install(tmp_path):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    dest = door_install.install_door(bin_dst, engine_root)

    assert dest == bin_dst / door_install.DOOR_INSTALLED_NAME
    assert door_install.is_door_installed(bin_dst) is True


def test_is_door_installed_does_not_raise_when_check_only_would():
    bin_dst_missing = "/nonexistent-path-for-door-install-test/bin"
    # `install_door(check_only=True)` would raise DoorInstallError here --
    # `is_door_installed` is the non-raising sibling, so it must return a
    # plain bool instead.
    result = door_install.is_door_installed(bin_dst_missing)
    assert result is False


def test_check_only_still_raises_on_absence(tmp_path):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(bin_dst, engine_root, check_only=True)


def test_install_writes_sidecar_alongside_installed_name(tmp_path):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    door_install.install_door(bin_dst, engine_root)

    assert (bin_dst / door_build.SIDECAR_FILENAME).exists()


def test_install_removes_shadowing_ps1_sibling(tmp_path):
    """2026-08-22 collision fix: `install_bin_forwarders` (substrate.py Step
    3b) derives a `coordinator-invoke.ps1` forwarder from the SAME
    `coordinator/bin/coordinator-invoke.py` the door replaces -- both land
    in the same settings-home `bin/` in a single `scripts/setup.py` run.
    PowerShell ranks a same-directory `.ps1` above a same-directory `.exe`,
    so a successful door install must clear that sibling rather than leave
    it standing (see door_install.py's module docstring, Windows
    paragraph)."""
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    shadow = bin_dst / f"{door_install.BARE_FORWARDER_NAME}.ps1"
    shadow.write_text("# stand-in for the generic .ps1 forwarder body\n", encoding="utf-8")

    # Ownership moved 2026-08-22: `install_door()` is the WINDOWS-only
    # path, so claiming the bare name from inside it was dead code on
    # POSIX. `scripts/setup.py :: install_warm_door` now calls
    # `claim_bare_name` once, after either branch lands a real door.
    # This test covers the helper; the real-path coverage lives in
    # scripts/test_setup.py :: test_install_warm_door_posix_branch_claims_the_bare_name.
    door_install.install_door(bin_dst, engine_root)
    assert shadow.exists(), (
        "install_door must NOT claim the bare name itself -- it is unreachable "
        "on POSIX, which is what made the original fix dead code there"
    )

    door_install.claim_bare_name(bin_dst)

    assert not shadow.exists()


def test_install_leaves_cmd_sibling_untouched(tmp_path):
    """`.cmd` is deliberately NOT removed -- PATHEXT ranks `.EXE` above
    `.CMD`, so a same-directory `coordinator-invoke.cmd` is unreachable by
    bare-name resolution once the door's binary sits beside it (see
    door_install.py's module docstring). Removing it would be a needless
    mutation of a file that is already harmless."""
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    cmd_sibling = bin_dst / f"{door_install.BARE_FORWARDER_NAME}.cmd"
    cmd_sibling.write_text("@echo off\r\n", encoding="utf-8")

    door_install.install_door(bin_dst, engine_root)

    assert cmd_sibling.exists()


def test_check_only_never_removes_shadowing_ps1_sibling(tmp_path):
    """`check_only` must never mutate `bin_dst` -- including the
    shadow-removal side effect, which only fires on a real write."""
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"stand-in door binary")
    (bin_dst / door_build.SIDECAR_FILENAME).write_text("x", encoding="utf-8")
    shadow = bin_dst / f"{door_install.BARE_FORWARDER_NAME}.ps1"
    shadow.write_text("# generic .ps1 forwarder body\n", encoding="utf-8")

    door_install.install_door(bin_dst, engine_root, check_only=True)

    assert shadow.exists()


# ---------------------------------------------------------------------------
# verify_installed_provenance -- five statuses
# ---------------------------------------------------------------------------


def test_verify_installed_provenance_no_door(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "no-door"


def test_verify_installed_provenance_absent_sidecar(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"door bytes")
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "absent"


def test_verify_installed_provenance_unrecorded(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"door bytes")
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps({"door_c_sha256": "deadbeef"}), encoding="utf-8"
    )
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "unrecorded"


def test_verify_installed_provenance_mismatch(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"door bytes")
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps({"image_sha256": "0" * 64}), encoding="utf-8"
    )
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "mismatch"
    assert "0" * 64 in verdict.detail


def test_verify_installed_provenance_ok(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door_bytes = b"door bytes"
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(door_bytes)
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps({"image_sha256": hashlib.sha256(door_bytes).hexdigest()}),
        encoding="utf-8",
    )
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "ok"


# ---------------------------------------------------------------------------
# install_door -- prebuilt/sidecar disagreement and stale-sidecar removal
# ---------------------------------------------------------------------------


def test_install_door_raises_when_prebuilt_exe_and_sidecar_disagree(tmp_path, monkeypatch):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    bad_provenance = tmp_path / "bad-provenance.json"
    bad_provenance.write_text(json.dumps({"image_sha256": "0" * 64}), encoding="utf-8")
    monkeypatch.setattr(door_install, "_PREBUILT_PROVENANCE", bad_provenance)

    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(bin_dst, engine_root)


def test_install_door_removes_stale_destination_sidecar_when_no_prebuilt_sidecar(
    tmp_path, monkeypatch
):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    stale_provenance = door_install.installed_provenance_path(bin_dst)
    stale_provenance.write_text(
        json.dumps({"image_sha256": "stale"}), encoding="utf-8"
    )

    missing_provenance = tmp_path / "missing-provenance.json"
    monkeypatch.setattr(door_install, "_PREBUILT_PROVENANCE", missing_provenance)

    door_install.install_door(bin_dst, engine_root)

    assert not stale_provenance.exists()
