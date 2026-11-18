"""door.exe resolves its cold-leg interpreter at run time: `door.python.txt` beside the
exe, else the `py -3` launcher. No interpreter path is compiled in, so a build box's
python.exe being absent elsewhere cannot make the cold fall-through exit 127.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.door.tests.test_warm_zero_door_runs_cold import (
    _install_invoke_door,
)
from coordinator_core.warm.tests.door_test_support import (
    FALLBACK_MARKER,
    WINDOWS_ONLY,
    make_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

_DOOR_C = Path(door_build.__file__).resolve().parent / "door.c"


def test_the_rendered_source_carries_no_interpreter_path(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    root.mkdir()
    rendered = door_build._render(_DOOR_C.read_text(encoding="utf-8"), root)
    assert "PYTHON_BIN_W" not in rendered
    assert "__PYTHON" not in rendered
    assert door_build._c_string_body(os.path.abspath(sys.executable)) not in rendered


def test_the_interpreter_sidecar_records_the_given_interpreter_as_one_line(tmp_path: Path) -> None:
    exe = tmp_path / "door.exe"
    interpreter = tmp_path / "py" / "python.exe"
    written = door_build.write_python_sidecar(exe, interpreter)
    assert written == tmp_path / door_build.PYTHON_SIDECAR_FILENAME
    assert written.read_bytes() == (os.path.abspath(interpreter) + "\n").encode("utf-8")


def test_the_interpreter_sidecar_defaults_to_the_running_interpreter(tmp_path: Path) -> None:
    written = door_build.write_python_sidecar(tmp_path / "door.exe")
    assert written.read_text(encoding="utf-8").strip() == os.path.abspath(sys.executable)


def test_an_unchanged_interpreter_sidecar_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "door.exe"
    door_build.write_python_sidecar(exe)

    def no_replace(*_a, **_k):
        raise AssertionError("an unchanged interpreter sidecar was rewritten")

    monkeypatch.setattr(door_build.os, "replace", no_replace)
    door_build.write_python_sidecar(exe)


def test_the_two_sidecar_names_match_door_c() -> None:
    source = _DOOR_C.read_text(encoding="utf-8")
    assert f'#define PYTHON_SIDECAR_FILENAME L"{door_build.PYTHON_SIDECAR_FILENAME}"' in source


def _cold_run(door: Path, root: Path, repo: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env["COORDINATOR_WARM"] = "0"
    return subprocess.run(
        [str(door), "ceremony.commit_v2", "--repo", str(repo)],
        input=b"{}", capture_output=True, env=env, timeout=60, cwd=str(repo),
        **no_console_creationflags(),
    )


@pytest.mark.spawns_process
@WINDOWS_ONLY
def test_a_sidecar_interpreter_runs_the_cold_leg(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)
    door_build.write_python_sidecar(door, Path(sys.executable))

    proc = _cold_run(door, root, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert FALLBACK_MARKER.encode() in proc.stdout


@pytest.mark.spawns_process
@WINDOWS_ONLY
def test_a_sidecar_naming_a_missing_interpreter_falls_back_to_the_py_launcher(tmp_path: Path) -> None:
    if shutil.which("py") is None:
        pytest.skip("py launcher not on PATH")
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)
    door_build.write_python_sidecar(door, tmp_path / "gone" / "python.exe")

    proc = _cold_run(door, root, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert FALLBACK_MARKER.encode() in proc.stdout


@pytest.mark.spawns_process
@WINDOWS_ONLY
def test_no_sidecar_runs_the_cold_leg_through_the_py_launcher(tmp_path: Path) -> None:
    if shutil.which("py") is None:
        pytest.skip("py launcher not on PATH")
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)
    assert not (door.parent / door_build.PYTHON_SIDECAR_FILENAME).exists()

    proc = _cold_run(door, root, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert FALLBACK_MARKER.encode() in proc.stdout


@pytest.mark.spawns_process
@WINDOWS_ONLY
def test_neither_a_sidecar_nor_the_launcher_still_refuses_with_exit_127(tmp_path: Path) -> None:
    # CreateProcessW also searches the Windows directories, outside PATH.
    if (Path(os.environ["SystemRoot"]) / "py.exe").exists():
        pytest.skip("py.exe is in the Windows directory; PATH cannot hide it")
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env["COORDINATOR_WARM"] = "0"
    env["PATH"] = str(tmp_path / "empty-path")

    proc = subprocess.run(
        [str(door), "ceremony.commit_v2", "--repo", str(tmp_path)],
        input=b"{}", capture_output=True, env=env, timeout=60, cwd=str(tmp_path),
        **no_console_creationflags(),
    )

    assert proc.returncode == 127
    assert b"could not launch the fallback" in proc.stderr
