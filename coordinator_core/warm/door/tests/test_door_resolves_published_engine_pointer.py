"""The door resolves this machine's published-engine pointer ahead of its baked root.

Ladder under test: override, `door.engine-root.txt` sidecar, then
`<settings-home>/machine-local/.claude-klabauter-root`, then the baked root,
then a refusal naming each rung. Each leg runs the real image with
`COORDINATOR_WARM=0`, so the cold fall-through's stub entrypoint prints
`FALLBACK_MARKER` only when the door resolved the stub root.
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
    FALLBACK_EXIT,
    FALLBACK_MARKER,
    WINDOWS_ONLY,
    door_default_entrypoint,
    make_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_POINTER_REL = Path("machine-local") / ".claude-klabauter-root"


def _env(settings_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORDINATOR_")}
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_home)
    env["COORDINATOR_WARM"] = "0"
    return env


def _write_pointer(settings_home: Path, value: str) -> None:
    pointer = settings_home / _POINTER_REL
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(value + "\n", encoding="utf-8")


def _run(exe: Path, settings_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(exe)], env=_env(settings_home), capture_output=True, text=True,
        timeout=60, **no_console_creationflags(),
    )


def _build_with_dead_baked_root(tmp_path: Path, name: str) -> Path:
    """A freshly built door whose baked root is no longer stamped and which has
    no sidecar, so only the runtime rungs can resolve."""
    baked = make_stub_engine_root(tmp_path / "baked")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / name
    door_build.build(baked, python_bin=Path(sys.executable), output=exe)
    (bin_dir / door_build.SIDECAR_FILENAME).unlink()
    (baked / "coordinator_core" / "_engine_stamp").unlink()
    return exe


@WINDOWS_ONLY
def test_committed_image_resolves_pointer_ahead_of_baked_root(tmp_path: Path) -> None:
    stub = make_stub_engine_root(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / (door_default_entrypoint() + ".exe")
    shutil.copy2(DOOR_EXE, exe)
    home = tmp_path / "home"
    _write_pointer(home, str(stub).replace("\\", "/"))

    result = _run(exe, home)

    assert FALLBACK_MARKER in result.stdout, result.stderr
    assert result.returncode == FALLBACK_EXIT


@WINDOWS_ONLY
def test_pointer_to_unstamped_dir_refuses_and_names_the_pointer(tmp_path: Path) -> None:
    exe = _build_with_dead_baked_root(tmp_path, door_default_entrypoint() + ".exe")
    home = tmp_path / "home"
    unstamped = tmp_path / "unstamped"
    unstamped.mkdir()
    _write_pointer(home, str(unstamped))

    result = _run(exe, home)

    assert result.returncode != 0
    assert FALLBACK_MARKER not in result.stdout
    assert str(unstamped) in result.stderr
    assert "is not a stamped engine root" in result.stderr
    assert "unset" in result.stderr


@WINDOWS_ONLY
def test_absent_pointer_refusal_names_each_rung(tmp_path: Path) -> None:
    exe = _build_with_dead_baked_root(tmp_path, door_default_entrypoint() + ".exe")
    home = tmp_path / "home"
    home.mkdir()

    result = _run(exe, home)

    assert result.returncode != 0
    assert FALLBACK_MARKER not in result.stdout
    assert "COORDINATOR_DOOR_ENGINE_ROOT unset" in result.stderr
    assert "sidecar absent" in result.stderr
    assert f"pointer {home / _POINTER_REL} absent" in result.stderr
    assert "baked" in result.stderr


@WINDOWS_ONLY
def test_valid_sidecar_wins_over_pointer(tmp_path: Path) -> None:
    exe = _build_with_dead_baked_root(tmp_path, door_default_entrypoint() + ".exe")
    sidecar_root = make_stub_engine_root(tmp_path / "sidecar-root")
    pointer_root = make_stub_engine_root(tmp_path / "pointer-root")
    marker_file = pointer_root / "coordinator" / "bin" / "coordinator-invoke.py"
    marker_file.write_text("raise SystemExit(7)\n", encoding="utf-8")
    door_build.write_sidecar(exe, sidecar_root)
    home = tmp_path / "home"
    _write_pointer(home, str(pointer_root))

    result = _run(exe, home)

    assert FALLBACK_MARKER in result.stdout, result.stderr
    assert result.returncode == FALLBACK_EXIT


def _posix_compiler() -> str | None:
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


@pytest.mark.skipif(os.name == "nt", reason="door_posix.c needs POSIX headers")
def test_posix_door_resolves_pointer_and_refuses_with_rungs(tmp_path: Path) -> None:
    compiler = _posix_compiler()
    if compiler is None:
        pytest.skip("no C compiler")
    from coordinator_core.warm.door import build_posix

    stub = make_stub_engine_root(tmp_path)
    exe = tmp_path / "bin" / door_default_entrypoint()
    exe.parent.mkdir()
    build_posix.build(stub, python_bin=Path(sys.executable), output=exe)
    sidecar = exe.parent / door_build.SIDECAR_FILENAME
    if sidecar.exists():
        sidecar.unlink()
    (stub / "coordinator_core" / "_engine_stamp").rename(tmp_path / "stamp.held")
    other = make_stub_engine_root(tmp_path / "other")
    home = tmp_path / "home"
    _write_pointer(home, str(other))

    ok = _run(exe, home)
    assert FALLBACK_MARKER in ok.stdout, ok.stderr
    assert ok.returncode == FALLBACK_EXIT

    unstamped = tmp_path / "unstamped"
    unstamped.mkdir()
    _write_pointer(home, str(unstamped))
    refused = _run(exe, home)
    assert refused.returncode != 0
    assert FALLBACK_MARKER not in refused.stdout
    assert str(unstamped) in refused.stderr
