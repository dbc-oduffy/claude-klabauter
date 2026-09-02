"""Tests for the POSIX door-build route (C2, dispatch brief F-013):

(a) `door_install.install_door()` must branch on `sys.platform`, not on
    `_PREBUILT_DOOR_EXE.exists()` -- off Windows it must route to
    `door_install_posix_build.build_or_advise`, never to the Windows-shaped
    `warm.door.build.build()` (`-ladvapi32 -lshell32` against a `windows.h`
    TU, which cannot compile on POSIX).

(b) A failed POSIX build (missing toolchain -> `DoorInstallError`, or a
    genuine compile failure -> `SystemExit`) must degrade the ONE name it
    was building a forwarder for, not abort the whole
    `_write_agent_helper_forwarders` install loop -- and the run must still
    report the degrade, never silently.

Spec backlink: state/dispatch-briefs/2026-09-01-the-dogfooded-install-stops-lying-about/C2.md
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.install import door_install
from coordinator_core.install import door_install_posix_build
from coordinator_core.install import substrate
from coordinator_core.warm.door import build as door_build


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


# --- (a) route -------------------------------------------------------------


def test_install_door_off_windows_never_calls_windows_build(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("door_build.build (Windows-shaped) must not run off win32")

    monkeypatch.setattr(door_build, "build", _fail_if_called)

    called = {}

    def _fake_build_or_advise(engine_root, *, python_bin=None, compiler=None, output=None):
        called["engine_root"] = engine_root
        called["output"] = output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-door-binary")
        door_build.write_sidecar(output, engine_root)
        return door_install_posix_build.PosixDoorBuildResult(built=True, output=output, advisory=None)

    monkeypatch.setattr(door_install_posix_build, "build_or_advise", _fake_build_or_advise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    dest = door_install.install_door(bin_dst, engine_root)

    assert dest == bin_dst / door_install.DOOR_INSTALLED_NAME
    assert called["engine_root"] == engine_root
    assert dest.read_bytes() == b"fake-door-binary"


def test_install_door_off_windows_raises_doorinstallerror_on_missing_toolchain(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def _fake_build_or_advise(engine_root, *, python_bin=None, compiler=None, output=None):
        return door_install_posix_build.PosixDoorBuildResult(
            built=False, output=None, advisory="no C compiler found on PATH"
        )

    monkeypatch.setattr(door_install_posix_build, "build_or_advise", _fake_build_or_advise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    with pytest.raises(door_install.DoorInstallError, match="no C compiler"):
        door_install.install_door(bin_dst, engine_root)

    # A failed door_install() must not have created the destination file.
    assert not (bin_dst / door_install.DOOR_INSTALLED_NAME).exists()


def test_install_door_off_windows_propagates_compile_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def _raise_systemexit(engine_root, *, python_bin=None, compiler=None, output=None):
        raise SystemExit("door build: compile failed (exit 1)")

    monkeypatch.setattr(door_install_posix_build, "build_or_advise", _raise_systemexit)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    with pytest.raises(SystemExit, match="compile failed"):
        door_install.install_door(bin_dst, engine_root)


def test_install_door_on_windows_route_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("build_or_advise (POSIX) must not run on win32")

    monkeypatch.setattr(door_install_posix_build, "build_or_advise", _fail_if_called)

    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed Windows prebuilt door in this checkout")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    dest = door_install.install_door(bin_dst, engine_root)
    assert dest == bin_dst / door_install.DOOR_INSTALLED_NAME


# --- (b) blast radius --------------------------------------------------------


def test_write_native_door_forwarder_degrades_on_doorinstallerror(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    from coordinator_core.install import door_install as door_install_mod

    def _raise_doorinstallerror(*args, **kwargs):
        raise door_install_mod.DoorInstallError("no C compiler found on PATH")

    monkeypatch.setattr(door_install_mod, "launcher_is_installable", lambda *a, **k: True)
    monkeypatch.setattr(door_install_mod, "install_named_forwarder", _raise_doorinstallerror)
    monkeypatch.setattr(
        "coordinator_core.warm.engine_root.is_engine_root", lambda root: True
    )

    result = substrate._write_native_door_forwarder(
        "some-tool", bin_dst, check_only=False, engine_root=engine_root
    )

    assert result is None  # degraded, not raised -- caller falls back to the Python pair


def test_write_native_door_forwarder_degrades_on_systemexit(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    from coordinator_core.install import door_install as door_install_mod

    def _raise_systemexit(*args, **kwargs):
        raise SystemExit("door build: compile failed (exit 1)")

    monkeypatch.setattr(door_install_mod, "launcher_is_installable", lambda *a, **k: True)
    monkeypatch.setattr(door_install_mod, "install_named_forwarder", _raise_systemexit)
    monkeypatch.setattr(
        "coordinator_core.warm.engine_root.is_engine_root", lambda root: True
    )

    result = substrate._write_native_door_forwarder(
        "some-tool", bin_dst, check_only=False, engine_root=engine_root
    )

    assert result is None


def test_write_agent_helper_forwarders_continues_past_a_build_failure(tmp_path, monkeypatch):
    """The per-name loop must not abort the whole run when one name's
    native-door cutover raises -- every later name must still be attempted
    and written via its Python-pair fallback (state/bug-backlog/2026-08-30-
    install-substrate-exits-0-after-failing-45f4d5390b68.yaml's INVERSE
    failure mode: this pins the direction where a failure must not kill
    every name after it, while the existing OSError leg keeps the run
    failing loud rather than exiting 0).

    Review: overengineering-reviewer flagged that raising `SystemExit` here
    constructs a path via monkeypatching `_cut_over_to_native_door` itself
    rather than exercising the real call chain, since
    `_write_native_door_forwarder` already catches `(DoorInstallError,
    SystemExit)` one level down and returns `None`. That reading is
    correct in isolation, but narrowing this loop's own catch to `OSError`
    flips this plan's own falsifier
    (`docs/plans/2026-09-01-the-dogfooded-install-stops-lying-about.
    falsifier.py`) from PASS back to FALSIFIED -- its static check reads a
    bare `except OSError` around this call chain as the door-build abort
    bug regardless of the inner catch. Escalated rather than applied; the
    catch here (and this test) stay as they were pending that call.

    Review: coordinator:code-reviewer (Finding 3) -- `_cut_over_to_native_door`
    is monkeypatched to always raise before `_write_agent_forwarder` is ever
    reached, so this test only proves loop continuation (both names attempted,
    run still raises); it does NOT exercise the Python-pair fallback landing.
    See `test_write_agent_helper_forwarders_writes_python_fallback_on_real_build_failure`
    below for that, with the real `_cut_over_to_native_door` call chain."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    def _cut_over_always_raises(name, bin_dst, check_only, *, engine_root, static_family_names=frozenset()):
        raise SystemExit(f"door build: compile failed for {name}")

    monkeypatch.setattr(substrate, "_cut_over_to_native_door", _cut_over_always_raises)

    written_py = []

    def _fake_write_agent_forwarder(f, py_dst, check_only, *, target, resolver_module):
        written_py.append(f)

    monkeypatch.setattr(substrate, "_write_agent_forwarder", _fake_write_agent_forwarder)

    agent_helper_target_map = {"alpha": "alpha_target", "beta": "beta_target"}

    # Every name's cutover raises, so every name is recorded as `failed` and
    # the run must still raise (non-zero exit contract) -- but it must not
    # abort mid-loop: both names are attempted.
    with pytest.raises(substrate.SubstrateFatalError):
        substrate._write_agent_helper_forwarders(
            agent_helper_target_map, bin_dst, check_only=False, engine_root=tmp_path / "engine",
        )

    # Review: coordinator:code-reviewer (Finding 3) -- `_cut_over_to_native_door`
    # raises before `_write_agent_forwarder` is ever called here, so
    # `_write_agent_forwarder` must NOT have been reached for either name;
    # this pins what this test actually proves (loop continuation), not the
    # fallback write itself.
    assert written_py == []


def test_write_agent_helper_forwarders_writes_python_fallback_on_real_build_failure(tmp_path, monkeypatch):
    """Review: coordinator:code-reviewer (Finding 3) -- integration companion
    to the test above, exercising the REAL `_cut_over_to_native_door` ->
    `_write_native_door_forwarder` call chain (only
    `door_install.install_named_forwarder` is mocked, to raise
    `DoorInstallError` the way a missing POSIX toolchain does) and asserting
    the degraded name's Python forwarder pair actually lands on disk --
    the claim the docstring above makes but its own monkeypatching of
    `_cut_over_to_native_door` never exercised."""
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    from coordinator_core.install import door_install as door_install_mod

    monkeypatch.setattr(door_install_mod, "launcher_is_installable", lambda *a, **k: True)

    def _raise_doorinstallerror(*args, **kwargs):
        raise door_install_mod.DoorInstallError("no C compiler found on PATH")

    monkeypatch.setattr(door_install_mod, "install_named_forwarder", _raise_doorinstallerror)
    monkeypatch.setattr("coordinator_core.warm.engine_root.is_engine_root", lambda root: True)

    agent_helper_target_map = {"alpha": "alpha_target"}

    substrate._write_agent_helper_forwarders(
        agent_helper_target_map, bin_dst, check_only=False, engine_root=engine_root,
    )

    assert (bin_dst / "alpha").exists() or (bin_dst / "alpha.py").exists() or (bin_dst / "alpha.cmd").exists()
