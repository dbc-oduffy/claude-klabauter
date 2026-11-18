"""A door fail-open line names the warm engine's stall-dump dir, read from <settings>/state/warm-stall-dir.txt.

The engine writes the pointer file; the door never hardcodes a drive path. Absent or empty pointer -> `stall_dir=unknown`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core.warm.door.tests.test_hook_mode_post_delivery_names_timeout import (  # noqa: F401 -- fixture
    _posix_hook_run,
    posix_door,
)
from coordinator_core.warm.tests.door_test_support import DOOR_POSIX_C, DOOR_WINDOWS_C
from coordinator_core.warm.tests.test_door_read_deadline_posix import runtime_base  # noqa: F401 -- fixture

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
_POINTER = "warm-stall-dir.txt"
_DOOR_DIR = Path(__file__).resolve().parents[1]


def _log_fn(source: Path) -> str:
    text = source.read_text(encoding="utf-8")
    fn = text[text.index("static void door_log_fail_open("):]
    return fn[: fn.index("\n}\n")]


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_both_doors_read_the_pointer_and_append_stall_dir_after_cause(source):
    fn = _log_fn(source)
    assert _POINTER in fn
    assert '"unknown"' in fn
    assert re.search(r"cause=%\.160s stall_dir=%\.\d+s\\n", fn)


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_no_drive_path_is_baked_into_the_stall_dir_lookup(source):
    assert not re.search(r"[A-Za-z]:[\\/]|AppData|stall-dumps", _log_fn(source))


@pytest.mark.skipif(os.name != "nt", reason="inspects the built Windows binaries")
@pytest.mark.parametrize("exe", ["door.exe", "door-w.exe"])
def test_the_built_windows_door_carries_the_stall_dir_field(exe):
    data = (_DOOR_DIR / exe).read_bytes()
    assert b"stall_dir=" in data
    assert _POINTER.encode("utf-16-le") in data


def _run_fail_open(posix_door, tmp_path, runtime_base, pointer: str | None) -> str:
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    settings = tmp_path / "settings"
    if pointer is not None:
        (settings / "state").mkdir(parents=True)
        (settings / "state" / _POINTER).write_text(pointer, encoding="utf-8")
    _posix_hook_run(posix_door, root, runtime_base, settings)
    (line,) = (settings / "state" / "door-fail-open.log").read_text(encoding="utf-8").splitlines()
    return line


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_a_present_pointer_is_named_in_the_fail_open_line(posix_door, tmp_path, runtime_base):
    line = _run_fail_open(posix_door, tmp_path, runtime_base, "/var/dumps/stall-dumps\n")
    assert line.endswith(" stall_dir=/var/dumps/stall-dumps")


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
@pytest.mark.parametrize("pointer", [None, "", "\n"], ids=["absent", "empty", "blank-line"])
def test_an_absent_or_empty_pointer_logs_unknown_and_still_fails_open(posix_door, tmp_path, runtime_base, pointer):
    line = _run_fail_open(posix_door, tmp_path, runtime_base, pointer)
    assert line.endswith(" stall_dir=unknown")
    assert " cause=" in line
