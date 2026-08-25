"""Tests for coordinator_core.install.door_install_posix_build: build when a
POSIX C toolchain is present, degrade to a runnable (never slash-command)
advisory when it isn't -- never raising on a toolchain miss.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C4.md
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from coordinator_core.install import door_install_posix_build as posix_install
from coordinator_core.warm.door import build_posix


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="door_install_posix_build is POSIX-only"
)


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def test_has_posix_compiler_matches_find_compiler(monkeypatch):
    # `has_posix_compiler` must agree with `build_posix._find_compiler` --
    # it is a non-raising wrapper around that same detector, not a second
    # probe with its own candidate list.
    try:
        build_posix._find_compiler(None)
        expect_hit = True
    except SystemExit:
        expect_hit = False
    assert posix_install.has_posix_compiler() is expect_hit


def test_has_posix_compiler_false_when_find_compiler_raises(monkeypatch):
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)
    assert posix_install.has_posix_compiler() is False


def test_build_or_advise_returns_advisory_on_toolchain_miss(tmp_path, monkeypatch):
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    result = posix_install.build_or_advise(engine_root)

    assert result.built is False
    assert result.output is None
    assert result.advisory is not None
    assert "python3 " in result.advisory
    assert "coordinator:" not in result.advisory


def test_build_or_advise_advisory_names_build_posix_script(tmp_path, monkeypatch):
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    result = posix_install.build_or_advise(engine_root)

    assert "build_posix.py" in result.advisory
    assert str(engine_root.resolve()) in result.advisory


def test_build_or_advise_builds_when_toolchain_present(tmp_path):
    if not posix_install.has_posix_compiler():
        pytest.skip("no C compiler on PATH in this environment")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    output = tmp_path / "door-out"

    result = posix_install.build_or_advise(engine_root, output=output)

    assert result.built is True
    assert result.advisory is None
    assert result.output == output
    assert output.exists()


def test_build_or_advise_output_is_executable_under_restrictive_umask(tmp_path):
    # `clang -O2 -o out t.c` under `umask 0177` writes `-rw-------` -- zero
    # exec bits at all, verified directly (2026-08-22). A restrictive umask
    # is ambient install-time state this best-effort/advisory POSIX path
    # must not depend on, so `build_or_advise` chmods the output itself
    # rather than trusting whatever the compiler happened to write.
    if not posix_install.has_posix_compiler():
        pytest.skip("no C compiler on PATH in this environment")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    output = tmp_path / "door-out"

    old_umask = os.umask(0o177)
    try:
        result = posix_install.build_or_advise(engine_root, output=output)
    finally:
        os.umask(old_umask)

    assert result.built is True
    mode = stat.S_IMODE(output.stat().st_mode)
    assert mode & stat.S_IXUSR, f"expected owner-exec bit set, got {oct(mode)}"


def test_build_or_advise_refuses_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    with pytest.raises(SystemExit):
        posix_install.build_or_advise(engine_root)
