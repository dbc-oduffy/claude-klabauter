"""`sizing-assemble --register -` / `--scout-register -` are decided pre-delivery.

The door's stdin is consumed by the door and never crosses the wire, so a
warm-served `<flag> -` leaves the engine CLI with nothing to read and the run
fails `missing required param: register`. The shared predicate
(`door_core.c :: door_invocation_declares_stdin_read`) routes exactly those
argvs cold. This test compiles the REAL `door_core.c` into a harness that
calls the predicate the doors call, with argv taken from the command line --
the closest in-process seam, with no server and no extra spawn on the warm path.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    DOOR_CORE_C,
    DOOR_POSIX_C,
    DOOR_WINDOWS_C,
    read,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

_HARNESS = r"""
#include <stdio.h>
#include "door_core.h"
int main(int argc, char **argv) {
    /* argv[1] is the basename; argv+1 is the door's argv (its argv[0] is the basename slot, its argv[1] the subcommand). */
    return door_invocation_declares_stdin_read(
        argv[1], argc - 1, (const char *const *)(argv + 1)) ? 10 : 0;
}
"""


@pytest.fixture(scope="module")
def predicate(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler on PATH to build the door_core harness")
    work = tmp_path_factory.mktemp("dash-route")
    src = work / "harness.c"
    src.write_text(_HARNESS, encoding="utf-8")
    exe = work / "harness"
    done = subprocess.run(
        [cc, "-std=c11", "-I", str(DOOR_CORE_C.parent), "-o", str(exe), str(src), str(DOOR_CORE_C)],
        capture_output=True,
        text=True,
        timeout=120,
        **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr

    def run(basename: str, *door_args: str) -> bool:
        proc = subprocess.run(
            [str(exe), basename, "door", *door_args],
            timeout=30,
            **no_console_creationflags(),
        )
        assert proc.returncode in (0, 10)
        return proc.returncode == 10

    return run


@pytest.mark.parametrize("flag", ["--register", "--scout-register"])
def test_dash_value_takes_the_cold_leg(predicate, flag):
    assert predicate("sizing-assemble", flag, "-", "--write", "s.yaml")
    assert predicate("sizing-assemble", "--write", "s.yaml", flag, "-")


@pytest.mark.parametrize("flag", ["--register", "--scout-register"])
def test_file_value_is_served_warm(predicate, flag):
    assert not predicate("sizing-assemble", flag, "o.yaml", "--write", "s.yaml")


def test_unrelated_invocations_stay_warm(predicate):
    assert not predicate("sizing-assemble", "--write", "s.yaml")
    assert not predicate("sizing-assemble", "--register")
    assert not predicate("sizing-assemble", "--relayed-ask-file", "-", "--write", "s.yaml")
    assert not predicate("ping", "--register", "-")


def test_both_doors_call_the_shared_invocation_predicate():
    assert "door_stdin_dash_flags" in read(DOOR_CORE_C)
    for door in (DOOR_WINDOWS_C, DOOR_POSIX_C):
        assert "door_invocation_declares_stdin_read(" in read(door)


def test_windows_door_hands_the_predicate_every_argv_slot():
    # The dash-flag scan walks argv up to argc; a fixed 2-slot array read with the real argc is out of bounds.
    src = read(DOOR_WINDOWS_C)
    body = src[src.index("static int door_basename_declares_stdin_read_w(") :]
    body = body[: body.index("\n}\n")]
    assert "argv_u8[2]" not in body
    assert "calloc((size_t)argc" in body
    assert "for (int i = 1; i < argc; i++) argv_u8[i] = wide_to_utf8(wargv[i], &len);" in body
