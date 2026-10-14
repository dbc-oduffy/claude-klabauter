"""COORDINATOR_DOOR_ENGINE_ROOT is the door's first rung and is normalized like the pointer rung.

Each leg runs the real image with `COORDINATOR_WARM=0`, so the stub entrypoint
prints `FALLBACK_MARKER` only when the door resolved the stub root. An unstamped
override is refused with the override named in stderr.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.tests.door_test_support import (
    DOOR_EXE,
    DOOR_POSIX_C,
    DOOR_WINDOWS_C,
    FALLBACK_EXIT,
    FALLBACK_MARKER,
    WINDOWS_ONLY,
    door_default_entrypoint,
    make_stub_engine_root,
    read,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_ENV = "COORDINATOR_DOOR_ENGINE_ROOT"


def _run(exe: Path, home: Path, root_value: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORDINATOR_")}
    env["COORDINATOR_SETTINGS_HOME"] = str(home)
    env["COORDINATOR_WARM"] = "0"
    env[_ENV] = root_value
    return subprocess.run(
        [str(exe)], env=env, capture_output=True, text=True,
        timeout=60, **no_console_creationflags(),
    )


def _dead_baked_door(tmp_path: Path, name: str) -> Path:
    baked = make_stub_engine_root(tmp_path / "baked")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / name
    door_build.build(baked, python_bin=Path(sys.executable), output=exe)
    (bin_dir / door_build.SIDECAR_FILENAME).unlink()
    (baked / "coordinator_core" / "_engine_stamp").unlink()
    return exe


@WINDOWS_ONLY
def test_committed_image_reaches_stamped_env_root_without_sidecar(tmp_path: Path) -> None:
    stub = make_stub_engine_root(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / (door_default_entrypoint() + ".exe")
    shutil.copy2(DOOR_EXE, exe)

    result = _run(exe, tmp_path / "home", str(stub))

    assert FALLBACK_MARKER in result.stdout, result.stderr
    assert result.returncode == FALLBACK_EXIT


@WINDOWS_ONLY
def test_forward_slash_env_root_resolves(tmp_path: Path) -> None:
    exe = _dead_baked_door(tmp_path, door_default_entrypoint() + ".exe")
    stub = make_stub_engine_root(tmp_path / "env-root")

    result = _run(exe, tmp_path / "home", str(stub).replace("\\", "/"))

    assert FALLBACK_MARKER in result.stdout, result.stderr
    assert result.returncode == FALLBACK_EXIT


@WINDOWS_ONLY
def test_unstamped_env_root_refuses_and_names_the_override(tmp_path: Path) -> None:
    exe = _dead_baked_door(tmp_path, door_default_entrypoint() + ".exe")
    unstamped = tmp_path / "unstamped"
    unstamped.mkdir()

    result = _run(exe, tmp_path / "home", str(unstamped))

    assert result.returncode != 0
    assert FALLBACK_MARKER not in result.stdout
    assert str(unstamped) in result.stderr
    assert _ENV in result.stderr


def test_both_doors_normalize_the_env_rung_like_the_pointer_rung() -> None:
    assert "env_buf[i] == L'/'" in read(DOOR_WINDOWS_C)
    assert "root[i] == '\\\\') root[i] = '/'" in read(DOOR_POSIX_C)
