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
from pathlib import Path

pytestmark = pytest.mark.complete

from coordinator_core.install import door_install
from coordinator_core.warm.door import build as door_build


def _stamp_engine_root(root) -> None:
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def test_door_installed_name_is_platform_resolved() -> None:
    if sys.platform == "win32":
        assert door_install.DOOR_INSTALLED_NAME == "coordinator-invoke.exe"
    else:
        assert door_install.DOOR_INSTALLED_NAME == "coordinator-invoke"


def test_prebuilt_lookup_and_provenance_match_platform_build_convention() -> None:
    if sys.platform == "win32":
        assert door_install._PREBUILT_DOOR_EXE.name == "door.exe"
        assert door_install._PREBUILT_PROVENANCE.name == "door.exe.provenance.json"
    else:
        assert door_install._PREBUILT_DOOR_EXE.name == "door"
        assert door_install._PREBUILT_PROVENANCE.name == "door.provenance.json"
    assert door_install._PREBUILT_DOOR_EXE.parent == door_install._PREBUILT_PROVENANCE.parent


def test_is_door_installed_false_on_absence(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    assert door_install.is_door_installed(bin_dst) is False


def test_is_door_installed_true_after_install(tmp_path: Path) -> None:
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    dest = door_install.install_door(bin_dst, engine_root)

    assert dest == bin_dst / door_install.DOOR_INSTALLED_NAME
    assert door_install.is_door_installed(bin_dst) is True


def test_is_door_installed_does_not_raise_when_check_only_would() -> None:
    bin_dst_missing = "/nonexistent-path-for-door-install-test/bin"
    result = door_install.is_door_installed(bin_dst_missing)
    assert result is False


def test_check_only_still_raises_on_absence(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(bin_dst, engine_root, check_only=True)


def test_install_writes_sidecar_alongside_installed_name(tmp_path: Path) -> None:
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    door_install.install_door(bin_dst, engine_root)

    assert (bin_dst / door_build.SIDECAR_FILENAME).exists()


def test_install_removes_shadowing_ps1_sibling(tmp_path: Path) -> None:
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


def test_install_leaves_cmd_sibling_untouched(tmp_path: Path) -> None:
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


def test_check_only_never_removes_shadowing_ps1_sibling(tmp_path: Path) -> None:
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


def test_verify_installed_provenance_python_forwarder_is_no_door(tmp_path: Path) -> None:
    (tmp_path / door_install.DOOR_INSTALLED_NAME).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    verdict = door_install.verify_installed_provenance(tmp_path)
    assert verdict.status == "no-door"


def test_verify_installed_provenance_no_door(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "no-door"


def test_verify_installed_provenance_absent_sidecar(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"\x7fELF door bytes")
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "absent"


def test_verify_installed_provenance_unrecorded(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"\x7fELF door bytes")
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps({"door_c_sha256": "deadbeef"}), encoding="utf-8"
    )
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "unrecorded"


def test_verify_installed_provenance_mismatch(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"\x7fELF door bytes")
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps({"image_sha256": "0" * 64}), encoding="utf-8"
    )
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "mismatch"
    assert "0" * 64 in verdict.detail


def test_verify_installed_provenance_ok(tmp_path: Path) -> None:
    """`ok` certifies BOTH that the sidecar describes the binary beside it AND
    that the binary is current. Planting arbitrary bytes with a matching hash
    is precisely the shape a build-behind install has -- self-consistent and
    stale -- and that shape is what let a door image predating the
    `COORDINATOR_DOOR_STDIN_MODE` gate read as healthy while `cross-repo-memo`
    hung (2026-09-01). The self-consistent-but-stale case is asserted separately
    in `tests/test_door_image_currency.py`.

    WHAT "CURRENT" MEANS IS PLATFORM-SPECIFIC, because only Windows ships a
    committed prebuilt to compare against -- the POSIX `door` is `.gitignore`d
    (`.gitignore:223`), so on POSIX currency is the sidecar's recorded source
    fingerprint. Keying the skip on `_PREBUILT_DOOR_EXE.exists()` rather than on
    the platform is what let this test read a developer's stale ignored build
    artifact as "the committed prebuilt" on every Mac."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    if sys.platform == "win32":
        if not door_install._PREBUILT_DOOR_EXE.exists():
            pytest.skip("no committed prebuilt door for this platform in this checkout")
        door_bytes = door_install._PREBUILT_DOOR_EXE.read_bytes()
        record = {"image_sha256": hashlib.sha256(door_bytes).hexdigest()}
    else:
        door_bytes = b"\x7fELF whatever this box compiled"
        record = {
            "image_sha256": hashlib.sha256(door_bytes).hexdigest(),
            "sources": door_install._current_source_fingerprint(),
        }
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(door_bytes)
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps(record), encoding="utf-8"
    )
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "ok", verdict


def _write_prebuilt_record(tmp_path, monkeypatch, record) -> None:
    path = tmp_path / "door.exe.provenance.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(door_install, "_WINDOWS_PREBUILT_PROVENANCE", path)


def _tree_sources() -> dict[str, str]:
    return {
        path.name: door_install.door_build.source_sha256(path)
        for path in door_install.door_build.SOURCES
    }


def test_prebuilt_built_from_this_trees_sources_has_no_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_prebuilt_record(tmp_path, monkeypatch, {"sources": _tree_sources()})
    assert door_install.committed_prebuilt_source_drift() == []


def test_prebuilt_built_from_older_sources_names_each_changed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = _tree_sources()
    sources["door.c"] = "0" * 64
    del sources["door_env_set.h"]
    _write_prebuilt_record(tmp_path, monkeypatch, {"sources": sources})
    assert door_install.committed_prebuilt_source_drift() == ["door.c", "door_env_set.h"]


@pytest.mark.parametrize("record", [None, {}, {"sources": {}}, ["not", "an", "object"]])
def test_prebuilt_drift_is_unanswerable_without_a_source_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record) -> None:
    if record is None:
        monkeypatch.setattr(
            door_install, "_WINDOWS_PREBUILT_PROVENANCE", tmp_path / "missing.json"
        )
    else:
        _write_prebuilt_record(tmp_path, monkeypatch, record)
    with pytest.raises(door_install.DoorInstallError):
        door_install.committed_prebuilt_source_drift()


def test_install_door_raises_when_prebuilt_exe_and_sidecar_disagree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PREBUILT-BRANCH TEST, SO PLATFORM IS THE SKIP KEY, NOT FILE PRESENCE.
    # `install_door` routes on platform first and POSIX never reads the prebuilt
    # pair at all -- it compiles. Skipping on `_PREBUILT_DOOR_EXE.exists()`
    # instead ran this Windows assertion down the POSIX build branch on any box
    # carrying the ignored local `door` artifact, where it can only ever report
    # DID NOT RAISE.
    if sys.platform != "win32":
        pytest.skip("install_door only reads the prebuilt exe/sidecar pair on Windows")
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    bad_provenance = tmp_path / "bad-provenance.json"
    bad_provenance.write_text(json.dumps({"image_sha256": "0" * 64}), encoding="utf-8")
    monkeypatch.setattr(door_install, "_PREBUILT_PROVENANCE", bad_provenance)
    monkeypatch.setattr(door_install, "committed_prebuilt_source_drift", lambda: [])

    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(bin_dst, engine_root)


def test_install_door_removes_stale_destination_sidecar_when_no_prebuilt_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_replace_possibly_running_image_identical_content_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"same-content")
    dest = tmp_path / "dest.exe"
    dest.write_bytes(b"same-content")

    before_mtime = dest.stat().st_mtime_ns

    door_install._replace_possibly_running_image(source, dest)

    assert dest.stat().st_mtime_ns == before_mtime
    assert list(tmp_path.glob("*.stale-*")) == []


def test_replace_possibly_running_image_copies_when_content_differs(tmp_path: Path) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"new-content")
    dest = tmp_path / "dest.exe"
    dest.write_bytes(b"old-content")

    door_install._replace_possibly_running_image(source, dest)

    assert dest.read_bytes() == b"new-content"


def test_replace_possibly_running_image_falls_through_to_copy_on_comparison_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"content-a")
    dest = tmp_path / "dest.exe"
    dest.write_bytes(b"content-a")

    monkeypatch.setattr(
        door_install.filecmp, "cmp", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )

    calls = []
    real_copy2 = door_install.shutil.copy2

    def _spy_copy2(src, dst):
        calls.append((src, dst))
        return real_copy2(src, dst)

    monkeypatch.setattr(door_install.shutil, "copy2", _spy_copy2)

    door_install._replace_possibly_running_image(source, dest)

    assert calls, "copy2 must be reached when the content comparison itself raises"


def test_sweep_displaced_images_removes_stale_siblings_only(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale_one = bin_dst / "coordinator-invoke.exe.stale-1234-5678"
    stale_two = bin_dst / "publish.exe.stale-999-111"
    keep = bin_dst / "coordinator-invoke.exe"
    stale_one.write_bytes(b"x")
    stale_two.write_bytes(b"x")
    keep.write_bytes(b"x")

    removed = door_install._sweep_displaced_images(bin_dst)

    assert set(removed) == {stale_one, stale_two}
    assert not stale_one.exists()
    assert not stale_two.exists()
    assert keep.exists()


def test_sweep_displaced_images_skips_permission_error_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    locked = bin_dst / "coordinator-invoke.exe.stale-1-1"
    unlockable = bin_dst / "publish.exe.stale-2-2"
    locked.write_bytes(b"x")
    unlockable.write_bytes(b"x")

    real_unlink = door_install.Path.unlink

    def _flaky_unlink(self, *args, **kwargs):
        if self.name == locked.name:
            raise PermissionError("still mapped")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(door_install.Path, "unlink", _flaky_unlink)

    removed = door_install._sweep_displaced_images(bin_dst)

    assert removed == [unlockable]
    assert locked.exists()
    assert not unlockable.exists()


def test_install_door_check_only_sweeps_nothing(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(b"x")
    (bin_dst / door_build.SIDECAR_FILENAME).write_text("{}", encoding="utf-8")
    stale = bin_dst / "coordinator-invoke.exe.stale-1-1"
    stale.write_bytes(b"x")

    door_install.install_door(bin_dst, engine_root, check_only=True)

    assert stale.exists()


def _unresolvable_fixture(tmp_path, manifest_text):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    engine_bin = tmp_path / "engine" / "coordinator" / "bin"
    engine_bin.mkdir(parents=True)
    (engine_bin / "gen-claude-author-shim.py").write_text("")
    (bin_dst / "_native-forwarder-manifest.json").write_text(manifest_text)
    for name in ("gen-claude-author-shim", "gen-claude-retired-shim", "unrecorded"):
        door_install.named_forwarder_path(bin_dst, name).write_bytes(b"x")
    return bin_dst, tmp_path / "engine"


def test_sweep_unresolvable_named_images_removes_only_recorded_dead_names(tmp_path: Path) -> None:
    manifest = json.dumps({"names": ["gen-claude-author-shim", "gen-claude-retired-shim"]})
    bin_dst, engine_root = _unresolvable_fixture(tmp_path, manifest)

    removed = door_install._sweep_unresolvable_named_images(bin_dst, engine_root)

    dead = door_install.named_forwarder_path(bin_dst, "gen-claude-retired-shim")
    assert removed == [dead]
    assert not dead.exists()
    assert door_install.named_forwarder_path(bin_dst, "gen-claude-author-shim").exists()
    assert door_install.named_forwarder_path(bin_dst, "unrecorded").exists()


def test_sweep_unresolvable_named_images_sweeps_nothing_on_unreadable_manifest(tmp_path: Path) -> None:
    bin_dst, engine_root = _unresolvable_fixture(tmp_path, "{not json")

    assert door_install._sweep_unresolvable_named_images(bin_dst, engine_root) == []
    assert door_install.named_forwarder_path(bin_dst, "gen-claude-retired-shim").exists()


def test_sweep_unresolvable_named_images_sweeps_nothing_for_a_root_without_bin(tmp_path: Path) -> None:
    manifest = json.dumps({"names": ["gen-claude-retired-shim"]})
    bin_dst, _ = _unresolvable_fixture(tmp_path, manifest)

    assert door_install._sweep_unresolvable_named_images(bin_dst, tmp_path / "nowhere") == []
    assert door_install.named_forwarder_path(bin_dst, "gen-claude-retired-shim").exists()


def test_sweep_unresolvable_named_images_keeps_an_extensionless_script(tmp_path: Path) -> None:
    manifest = json.dumps({"names": ["static-check"]})
    bin_dst, engine_root = _unresolvable_fixture(tmp_path, manifest)
    (engine_root / "coordinator" / "bin" / "static-check").write_text("")
    door_install.named_forwarder_path(bin_dst, "static-check").write_bytes(b"x")

    assert door_install._sweep_unresolvable_named_images(bin_dst, engine_root) == []


# --- windowless image (door-w.exe) ---------------------------------------------------------

import os
import struct

_WIN = pytest.mark.skipif(sys.platform != "win32", reason="windowless image is a Windows artifact")
_MZ = b"MZ" + b"\x00" * 64


def _pe_subsystem(path):
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    return struct.unpack_from("<H", data, pe + 24 + 68)[0]


def test_windowless_link_flags_for_both_compilers() -> None:
    clang = door_build._compile_flags("clang", True)
    assert clang[-4:] == ["-Xlinker", "/SUBSYSTEM:WINDOWS", "-Xlinker", "/ENTRY:mainCRTStartup"]
    cl = door_build._compile_flags("cl", True)
    assert cl[-2:] == ["/SUBSYSTEM:WINDOWS", "/ENTRY:mainCRTStartup"]
    for kind in ("clang", "cl"):
        assert not any("SUBSYSTEM" in f for f in door_build._compile_flags(kind, False))


@pytest.mark.parametrize("kind", ["clang", "cl"])
def test_compile_passes_subsystem_only_for_the_windowless_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind) -> None:
    seen = []

    def _run(cmd, **kwargs):
        seen.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(door_build.subprocess, "run", _run)
    door_build._compile(kind, "cc", tmp_path / "a.c", tmp_path / "a.exe", windowless=True)
    door_build._compile(kind, "cc", tmp_path / "a.c", tmp_path / "b.exe")
    assert any("/SUBSYSTEM:WINDOWS" in a for a in seen[0])
    assert any("/ENTRY:mainCRTStartup" in a for a in seen[0])
    assert not any("SUBSYSTEM" in a for a in seen[1])


@_WIN
def test_committed_images_are_gui_and_console_subsystems() -> None:
    if not door_install._PREBUILT_WINDOWLESS_EXE.exists():
        pytest.skip("no committed door-w.exe")
    assert _pe_subsystem(door_install._PREBUILT_WINDOWLESS_EXE) == 2
    assert _pe_subsystem(door_install._PREBUILT_DOOR_EXE) == 3
    record = json.loads(door_install._WINDOWLESS_PREBUILT_PROVENANCE.read_text(encoding="utf-8"))
    assert record["image_sha256"] == hashlib.sha256(door_install._PREBUILT_WINDOWLESS_EXE.read_bytes()).hexdigest()
    assert "/SUBSYSTEM:WINDOWS" in record["compile_flags"]


def test_windowless_names_are_hook_run_on_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(door_install.sys, "platform", "win32")
    assert door_install.name_gets_windowless_image("hook-run") is True
    assert door_install.name_gets_windowless_image("cross-repo-memo") is False
    monkeypatch.setattr(door_install.sys, "platform", "linux")
    assert door_install.name_gets_windowless_image("hook-run") is False


@_WIN
def test_install_door_lands_the_windowless_image_and_provenance_beside_the_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not door_install._PREBUILT_WINDOWLESS_EXE.exists():
        pytest.skip("no committed door-w.exe")
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    monkeypatch.setattr(door_install, "committed_prebuilt_source_drift", lambda: [])

    door_install.install_door(bin_dst, engine_root)

    landed = door_install.windowless_image_path(bin_dst)
    assert landed.read_bytes() == door_install._PREBUILT_WINDOWLESS_EXE.read_bytes()
    assert (bin_dst / "door-w.exe.provenance.json").exists()
    assert (bin_dst / door_install.DOOR_INSTALLED_NAME).read_bytes() == door_install._PREBUILT_DOOR_EXE.read_bytes()


@_WIN
def test_hook_run_links_the_windowless_image_and_every_other_name_the_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(_MZ + b"door")
    windowless = door_install.windowless_image_path(bin_dst)
    windowless.write_bytes(_MZ + b"windowless")

    hook = door_install.install_named_forwarder(bin_dst, tmp_path, "hook-run", source=door)
    other = door_install.install_named_forwarder(bin_dst, tmp_path, "cross-repo-memo", source=door)

    assert os.path.samefile(hook, windowless)
    assert os.path.samefile(other, door)
    assert not os.path.samefile(hook, door)


@_WIN
def test_hook_run_falls_back_to_the_door_when_no_windowless_image_is_installed(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(_MZ + b"door")

    hook = door_install.install_named_forwarder(bin_dst, tmp_path, "hook-run", source=door)

    assert os.path.samefile(hook, door)


@_WIN
def test_audit_judges_hook_run_against_the_windowless_image(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(_MZ + b"door")
    windowless = door_install.windowless_image_path(bin_dst)
    windowless.write_bytes(_MZ + b"windowless")
    other = door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    os.link(door, other)
    hook = door_install.named_forwarder_path(bin_dst, "hook-run")
    os.link(windowless, hook)

    audit = door_install.audit_installed_image_currency(bin_dst, ["hook-run", "cross-repo-memo"])
    assert sorted(audit.current) == ["cross-repo-memo", "hook-run"]
    assert audit.stale == []

    hook.unlink()
    os.link(door, hook)
    audit = door_install.audit_installed_image_currency(bin_dst, ["hook-run", "cross-repo-memo"])
    assert audit.stale == ["hook-run"]
    assert audit.current == ["cross-repo-memo"]


@_WIN
def test_audit_reads_a_windowless_slot_stale_when_no_windowless_image_is_installed(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(_MZ + b"door")
    os.link(door, door_install.named_forwarder_path(bin_dst, "hook-run"))

    assert door_install.audit_installed_image_currency(bin_dst, ["hook-run"]).stale == ["hook-run"]


@_WIN
def test_replacing_the_door_relinks_hook_run_to_the_windowless_image_not_the_door(tmp_path: Path) -> None:
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(_MZ + b"old-door")
    windowless = door_install.windowless_image_path(bin_dst)
    windowless.write_bytes(_MZ + b"windowless")
    hook = door_install.named_forwarder_path(bin_dst, "hook-run")
    other = door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    os.link(door, hook)
    os.link(door, other)
    prior = door_install._file_identity(door)
    door.unlink()
    door.write_bytes(_MZ + b"new-door")

    moved = door_install._relink_names_stranded_on(bin_dst, door, prior)

    assert moved == 2
    assert os.path.samefile(other, door)
    assert os.path.samefile(hook, windowless)


def test_relink_never_touches_sidecars_even_when_one_shares_the_replaced_identity(tmp_path: Path) -> None:
    # A freed inode number is recycled by the next file written, so a sidecar
    # can carry the replaced image's identity; it must stay a plain data file.
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / door_install.DOOR_INSTALLED_NAME
    door.write_bytes(b"new-door")
    protected = [
        bin_dst / door_build.SIDECAR_FILENAME,
        bin_dst / door_build.PYTHON_SIDECAR_FILENAME,
        bin_dst / (door_install.DOOR_INSTALLED_NAME + ".provenance.json"),
    ]
    protected[0].write_text("data\n", encoding="utf-8")
    for p in protected[1:]:
        os.link(protected[0], p)
    prior = door_install._file_identity(protected[0])

    door_install._relink_names_stranded_on(bin_dst, door, prior)

    for p in protected:
        assert not os.path.samefile(p, door)
    assert protected[0].read_text(encoding="utf-8") == "data\n"


def test_file_identity_is_the_entry_itself_not_its_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_bytes(b"x")
    link = tmp_path / "l"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert door_install._file_identity(link) != door_install._file_identity(target)


@_WIN
def test_drift_covers_the_windowless_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {p.name: door_build.source_sha256(p) for p in door_build.SOURCES}
    door_prov = tmp_path / "door.exe.provenance.json"
    door_prov.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    behind = dict(sources, **{"door_core.c": "0" * 64})
    (tmp_path / "door-w.exe.provenance.json").write_text(json.dumps({"sources": behind}), encoding="utf-8")

    assert door_install.committed_prebuilt_source_drift(provenance_path=door_prov) == ["door_core.c"]

    (tmp_path / "door-w.exe.provenance.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
    assert door_install.committed_prebuilt_source_drift(provenance_path=door_prov) == []


@_WIN
def test_mirror_rebuild_ships_both_images(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    door_dir = mirror / "coordinator_core" / "warm" / "door"
    door_dir.mkdir(parents=True)
    (door_dir / "door.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    (door_dir / "build.py").write_text(
        "import json, hashlib\n"
        "from pathlib import Path\n"
        "_PROVENANCE_SUFFIX = '.provenance.json'\n"
        "SOURCES = (Path(__file__).parent / 'door.c',)\n"
        "def build(engine_root, *, python_bin=None, compiler=None, output=None):\n"
        "    src = {p.name: hashlib.sha256(p.read_bytes().replace(b'\\r', b'')).hexdigest() for p in SOURCES}\n"
        "    for name in (output.name, 'door-w.exe'):\n"
        "        (output.parent / name).write_bytes(b'MZ' + name.encode())\n"
        "        (output.parent / (name + _PROVENANCE_SUFFIX)).write_text(json.dumps({'sources': src}))\n"
        "    return output\n",
        encoding="utf-8",
    )
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    assert door_install.rebuild_door_for_mirror(mirror, engine_root) is True

    assert (door_dir / "door-w.exe").read_bytes() == b"MZdoor-w.exe"
    assert (door_dir / "door-w.exe.provenance.json").exists()
    assert (door_dir / "door.exe").exists()
