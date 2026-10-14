"""C4b -- the door's fall-through diagnostic is opt-in, never default.

`door.c :: fall_through` degrades to cold silently on the ordinary path. The
original gap (a 13x slow path that went unnoticed because it announced
nothing) is closed by a diagnostic channel, not by default output: with
`COORDINATOR_DOOR_DEBUG=1` the door prints one "falling through" line to
STDERR (never stdout -- a programmatic consumer parsing the relayed CLI's own
stdout as data must not see it corrupted) before spawning the fallback.
Without the var the ordinary degrade prints nothing: a native stderr line is
a terminating error under Windows PowerShell 5.1 (commit c25b589251), so the
debug var is the diagnostic channel. Two genuinely fatal cases
(no Python interpreter reachable, no published engine resolvable) diagnose
regardless. In every case the exit code stays exactly the dispatched CLI's.

Telemetry (`op_latency`) was considered and rejected as the venue:
`door_route_signal.py`'s own docstring records that the door protocol
carries neither `_origin_worktree` nor `_caller_cwd`, so a degrade recorded
there lands wherever the EXECUTING process's cwd happens to resolve the sink
to -- not somewhere an operator can find it. `test_pre_delivery_failure_still_
falls_through` (test_door_read_deadline.py) proves a fall-through reaches the
fallback and produces no ENVELOPE of its own; this file proves the debug line
is present on that same ordinary path, absent by default, and that neither
touches the exit code.

NO LIVE WARM SERVER IS INVOLVED. Nothing listens on the derived pipe name
for this throwaway stub engine root, so the connect fails before any byte
is written -- the same "ordinary doubt" shape `test_door_read_deadline.py`
exercises, reused here rather than re-invented.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name != "nt", reason="door.exe is a Windows binary"),
]

_DOOR_DIR = Path(__file__).resolve().parents[1] / "door"
_DOOR_EXE = _DOOR_DIR / "door.exe"
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_FALLBACK_MARKER = "FALL-THROUGH-WARN-TEST-FALLBACK-RAN"
_FALLBACK_EXIT = 17


def _make_stub_engine_root(tmp_path: Path) -> Path:
    root = tmp_path / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"fall-through-warns-test-{os.getpid()}-{time.time_ns()}\n",
        encoding="utf-8",
    )
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "coordinator-invoke.py").write_text(
        "import sys\n"
        f"print({_FALLBACK_MARKER!r})\n"
        f"raise SystemExit({_FALLBACK_EXIT})\n",
        encoding="utf-8",
    )
    return root.resolve()


def _default_named_door(tmp_path: Path) -> Path:
    """A copy of the door image under the DEFAULT installed name.

    C0 made the cold leg name-aware: `fall_through` targets
    `coordinator/bin/<own-basename>.py` and refuses by name when that script
    is absent. The repo's own image is literally `door.exe`, so invoking it
    in place resolves the basename `door` and correctly refuses — there is no
    `coordinator/bin/door.py`. This chunk is about the ORDINARY degrade, which
    is the one taken under the default name, so the image must carry that name
    for the fall-through to be reached at all.
    """
    named = tmp_path / "coordinator-invoke.exe"
    shutil.copyfile(_DOOR_EXE, named)
    return named


def _run_door(
    engine_root: Path, door_exe: Path, *, debug: bool = False
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    env.pop("COORDINATOR_DOOR_DEBUG", None)
    if debug:
        env["COORDINATOR_DOOR_DEBUG"] = "1"
    return subprocess.run(
        [str(door_exe), "ping"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=str(engine_root),
        creationflags=_NO_CONSOLE,
    )


def test_ordinary_fall_through_warns_on_stderr(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    proc = _run_door(root, _default_named_door(tmp_path), debug=True)

    assert _FALLBACK_MARKER in proc.stdout, (
        "the fallback must actually have run for this test to prove anything"
    )
    assert proc.returncode == _FALLBACK_EXIT, (
        "a warn on the degrade must never change the relayed exit code"
    )
    assert "falling through" in proc.stderr, (
        "an ordinary degrade to cold must print a warn to stderr -- "
        "silence here is the exact gap this chunk exists to close"
    )
    assert _FALLBACK_MARKER not in proc.stderr, (
        "the warn must never corrupt a programmatic consumer's stdout -- "
        "it belongs on stderr only, distinct from the relayed CLI's own output"
    )


def test_ordinary_fall_through_is_silent_by_default(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    proc = _run_door(root, _default_named_door(tmp_path))

    assert "falling through" not in proc.stderr
    assert proc.returncode == _FALLBACK_EXIT


def test_warn_does_not_leak_onto_stdout(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    proc = _run_door(root, _default_named_door(tmp_path))

    assert "door:" not in proc.stdout, (
        "the door's own diagnostic prefix must never appear on stdout -- "
        "stdout is reserved for the relayed CLI's own output"
    )
