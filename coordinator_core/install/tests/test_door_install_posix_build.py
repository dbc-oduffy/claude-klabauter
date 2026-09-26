
from __future__ import annotations

import os
import stat
import subprocess
import sys

from pathlib import Path

import pytest

from coordinator_core.install import door_install_posix_build as posix_install
from coordinator_core.warm.door import build_posix
from coordinator_core.win_portability import no_console_creationflags


_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="door_install_posix_build is POSIX-only"
)


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def test_has_posix_compiler_matches_find_compiler(monkeypatch):
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


def test_build_or_advise_advisory_command_actually_runs(tmp_path, monkeypatch):
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    result = posix_install.build_or_advise(engine_root)

    assert str(engine_root.resolve()) in result.advisory

    marker = "build it later with: "
    assert marker in result.advisory
    command = result.advisory.split(marker, 1)[1].strip()
    tokens = command.split()

    # advisory carries `PYTHONPATH=<engine root>` because the module route
    env_prefix: "dict[str, str]" = {}
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        key, _, value = tokens[0].partition("=")
        env_prefix[key] = value
        tokens = tokens[1:]

    argv = tokens
    assert argv[0] == "python3"
    assert env_prefix.get("PYTHONPATH") == str(engine_root.resolve()), (
        "the advisory must make the engine root importable, or the module route "
        f"it names cannot resolve: {command}"
    )

    assert ".py" not in command, f"advisory must not name a file path: {command}"
    assert argv[1:3] == ["-m", "coordinator_core.warm.door.build_posix"], argv
    argv[0] = sys.executable

    completed = subprocess.run(
        [*argv[:-1], "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
        # Run the command's own SHAPE -- `PYTHONPATH=<root> python3 -m ...` --
        # The PYTHONPATH value is swapped to the real engine root for the
        # accident, so PYTHONPATH is the only thing that can resolve it.
        env={
            **os.environ,
            **env_prefix,
            "PYTHONPATH": str(_REPO_ROOT),
        },
        **no_console_creationflags(),
    )
    assert completed.returncode == 0, (
        "the advisory names a command that does not run:\n"
        f"  command: {command}\n"
        f"  stderr:  {completed.stderr}"
    )
    assert "engine_root" in completed.stdout


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
