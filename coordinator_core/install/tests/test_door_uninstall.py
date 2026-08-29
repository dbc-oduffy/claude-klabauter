"""Tests for coordinator_core.install.door_uninstall: it must remove
exactly what `door_install.install_door()` writes -- the platform-resolved
door binary, its provenance sidecar, and the shared engine-root sidecar --
and nothing else (in particular, never `engine.warm.enabled`), and it must
re-emit the fallback `coordinator-invoke` forwarder the door claimed/
shadowed-out so a real removal never leaves the bare name unreachable.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C9.md
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.install import door_install, door_uninstall
from coordinator_core.install.door_uninstall import _UNINSTALL_FALLBACK_CMD_MARKER
from coordinator_core.install.substrate import _AGENT_FORWARDER_MARKER
from coordinator_core.warm.door import build as door_build


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def test_uninstall_is_noop_on_never_installed_bin_dst(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    removed = door_uninstall.uninstall_door(bin_dst)
    assert removed == []


def test_uninstall_is_noop_on_absent_bin_dst(tmp_path):
    bin_dst = tmp_path / "does-not-exist" / "bin"
    removed = door_uninstall.uninstall_door(bin_dst)
    assert removed == []


def test_uninstall_removes_installed_door_and_sidecars(tmp_path):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    door_install.install_door(bin_dst, engine_root)
    assert door_install.is_door_installed(bin_dst) is True

    removed = door_uninstall.uninstall_door(bin_dst)

    # The removal itself: the sidecar is never re-created, so its absence
    # alone already proves the door is gone (`is_door_installed` needs both
    # the exe AND the sidecar).
    assert not (bin_dst / door_build.SIDECAR_FILENAME).exists()
    assert door_install.is_door_installed(bin_dst) is False
    assert (bin_dst / door_install.DOOR_INSTALLED_NAME) in removed
    assert (bin_dst / door_build.SIDECAR_FILENAME) in removed

    # AC10 (this fix): uninstalling the door must not leave `bin_dst` with
    # no `coordinator-invoke` reachable at all -- see door_install.py's
    # module docstring and `door_uninstall._reemit_fallback_forwarder`.
    # On POSIX `BARE_FORWARDER_NAME` and `DOOR_INSTALLED_NAME` are the
    # identical bare string, so this is the SAME path the assertion above
    # would previously have required to be absent -- it is populated again,
    # now with the plain-Python forwarder body, not the door's binary.
    fallback = bin_dst / door_install.BARE_FORWARDER_NAME
    assert fallback.is_file()
    assert _AGENT_FORWARDER_MARKER in fallback.read_text(encoding="utf-8")
    # PATHEXT-resolvable sibling: `.cmd`, not `.ps1` (docs/plans/2026-08-26-
    # every-forwarder-that-can-reach-the-door-does.md C12 -- default
    # Windows PATHEXT carries no `.PS1`, so a `.ps1` fallback here would
    # never discharge door_install.py's Hard Invariant 1 for a `cmd.exe` or
    # bare-`CreateProcess` caller).
    cmd_fallback = bin_dst / f"{door_install.BARE_FORWARDER_NAME}.cmd"
    assert cmd_fallback.is_file()
    assert _UNINSTALL_FALLBACK_CMD_MARKER in cmd_fallback.read_text(encoding="utf-8")


def test_uninstall_is_idempotent(tmp_path):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    door_install.install_door(bin_dst, engine_root)
    first = door_uninstall.uninstall_door(bin_dst)
    assert first != []

    second = door_uninstall.uninstall_door(bin_dst)
    assert second == []


def test_uninstall_leaves_other_bin_contents_untouched(tmp_path):
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    door_install.install_door(bin_dst, engine_root)
    unrelated = bin_dst / "some-other-forwarder"
    unrelated.write_text("keep me", encoding="utf-8")

    door_uninstall.uninstall_door(bin_dst)

    assert unrelated.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_module_is_runnable_as_a_script(tmp_path, capsys):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    rc = door_uninstall.main(["--bin-dst", str(bin_dst)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to remove" in out
