"""Shared fixtures for the Windows-door test suites: the stub engine root, the
stub named-pipe server, the door-source readers, and the gate's engine-root
resolution.

Public names on purpose. Sibling door tests import from here rather than from
each other's `test_*.py`, so renaming a symbol inside one suite cannot break
another suite at import time. The POSIX twins keep their own socket-shaped
helpers in `test_door_read_deadline_posix.py`.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.warm.engine_root import is_engine_root

DOOR_DIR = Path(__file__).resolve().parents[1] / "door"
DOOR_EXE = DOOR_DIR / "door.exe"
DOOR_WINDOWS_C = DOOR_DIR / "door.c"
DOOR_POSIX_C = DOOR_DIR / "door_posix.c"
DOOR_CORE_C = DOOR_DIR / "door_core.c"
DOOR_CORE_H = DOOR_DIR / "door_core.h"

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="door.exe is a Windows binary"
)

#: Printed by the stub `coordinator-invoke.py` the door falls through to.
#: Its presence is proof of a fallback spawn; its absence, proof of a refusal.
FALLBACK_MARKER = "DOOR-TEST-FALLBACK-RAN"
FALLBACK_EXIT = 42

ENGINE_ROOT_OVERRIDE_ENV = "COORDINATOR_WARM_GATE_ENGINE_ROOT"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def candidate_source_roots() -> list[Path]:
    override = os.environ.get(ENGINE_ROOT_OVERRIDE_ENV)
    if override:
        return [Path(override)]
    live_tree_root = Path(__file__).resolve().parents[3]
    return [live_tree_root.parent / "claude-klabauter"]


def resolve_stamped_source_root() -> Optional[Path]:
    for candidate in candidate_source_roots():
        if candidate.is_dir() and is_engine_root(candidate):
            return candidate
    return None


def door_default_entrypoint() -> str:
    """The door's default entrypoint name, read out of `door.c` for the same
    reason the deadlines are: the shipped binary compares its own basename
    against this constant, and a literal here can drift away from it."""
    source = DOOR_WINDOWS_C.read_text(encoding="utf-8")
    match = re.search(r'^#define\s+DOOR_DEFAULT_ENTRYPOINT_W\s+L"([^"]+)"\s*$', source, re.MULTILINE)
    assert match, f"DOOR_DEFAULT_ENTRYPOINT_W not found in {DOOR_WINDOWS_C} -- renamed or removed"
    return match.group(1)


def make_stub_engine_root(tmp_path: Path) -> Path:
    """A throwaway directory shaped enough like a published engine for the
    door to accept it: a non-empty `coordinator_core/_engine_stamp` (what
    `is_valid_engine_root_w` checks) plus a `coordinator/bin/
    coordinator-invoke.py` that announces itself instead of running an op.

    The stamp bytes are unique per test run, so `compute_client_token` yields
    a token -- and therefore a pipe name -- no other process on this box is
    using."""
    root = tmp_path / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"door-read-deadline-test-{os.getpid()}-{time.time_ns()}\n", encoding="utf-8"
    )
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "coordinator-invoke.py").write_text(
        "import sys\n"
        f"print({FALLBACK_MARKER!r})\n"
        f"raise SystemExit({FALLBACK_EXIT})\n",
        encoding="utf-8",
    )
    return root.resolve()


def pipe_name_for(root: Path) -> str:
    from coordinator_core.warm import election, skew

    return election.pipe_name(
        skew.compute_client_token(root), engine_clone=root
    )


class ReplyingServer:
    """A named pipe that reads one request frame and answers it verbatim
    with `reply`.

    This is the control the deadline tests need to be worth anything: the
    read mechanism changed from a synchronous `ReadFile` to overlapped I/O,
    and a bound on a read that no longer reads correctly is not a fix. These
    exercise the ORDINARY outcomes -- a success envelope and a
    provably-undispatched error -- through the new mechanism, end to end,
    with no live warm server involved."""

    def __init__(self, name: str, reply: bytes) -> None:
        import _winapi
        from coordinator_core.warm import election

        self._winapi = _winapi
        self._handle = election.elect(name)
        self._reply = reply
        self.request = b""
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._winapi.ConnectNamedPipe(self._handle, False)
            while b"\n" not in self.request:
                chunk, _ = self._winapi.ReadFile(self._handle, 65536)
                if not chunk:
                    return
                self.request += chunk
            self._winapi.WriteFile(self._handle, self._reply)
        except OSError:
            pass

    def close(self) -> None:
        self._thread.join(timeout=10)
        try:
            self._winapi.CloseHandle(self._handle)
        except OSError:
            pass


def door_under_default_name(engine_root: Path) -> Path:
    """A copy of the built door at `coordinator-invoke.exe`, the name these
    tests' fixture is written against.

    `door.c` C0 made the door name-aware: `resolve_own_basename` reads the
    running image's own basename, `fall_through` targets
    `<basename>.py` in `coordinator/bin/`, and any basename other than
    the door's default entrypoint name also puts `entrypoint` on the wire. The build
    artifact is named `door.exe`, so invoking it directly exercises neither
    the cold script the stub root provides nor the default-name wire frame --
    it looks for a `coordinator/bin/door.py` that no tree has. Production
    installs the image under its entrypoint's name; so does this.
    """
    installed = engine_root / (door_default_entrypoint() + ".exe")
    if not installed.exists():
        shutil.copy2(DOOR_EXE, installed)
    return installed
