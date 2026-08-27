"""C4b -- the door's fall-through is no longer silent by contract.

WHAT REGRESSED, AND WHY THIS FILE EXISTS. `door.c :: fall_through` printed
NOTHING on the ordinary degrade-to-cold path (README.md's own "the ordinary
fallback path prints nothing" line) and emitted a diagnostic in exactly two
genuinely fatal cases: no Python interpreter reachable at all, and no
published engine resolvable by any means. The original baton for this
workstream named that silence as the root cause of the whole gap it exists
to close: "The door's fall-through prints nothing by contract. That is
defensible for a fast path and indefensible as the only signal." The 13x
slow-path gap went unnoticed for as long as it did because the slow path
announced nothing.

THE FIX proved here: every ordinary degrade to cold now prints one line to
STDERR (never stdout -- a programmatic consumer parsing the relayed CLI's
own stdout as data must not see it corrupted) before spawning the fallback,
and the exit code stays exactly the dispatched CLI's own -- a warn is not a
failure. Telemetry (`op_latency`) was considered and rejected as the venue:
`door_route_signal.py`'s own docstring records that the door protocol
carries neither `_origin_worktree` nor `_caller_cwd`, so a degrade recorded
there lands wherever the EXECUTING process's cwd happens to resolve the sink
to -- not somewhere an operator watching stderr can find it. This is a
positive assertion, not omission: `test_pre_delivery_failure_still_falls_
through` (test_door_read_deadline.py) already proves a fall-through reaches
the fallback and produces no ENVELOPE of its own; this file proves the NEW
stderr line is actually present on that same ordinary path, and that the
exit code is untouched by adding it.

NEGATIVE SPEC: this is not a rate limiter, a suppression flag, or a
"once per session" cleverness. Every degrade prints, unconditionally.

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

#: Printed by the stub `coordinator-invoke.py` the door falls through to.
_FALLBACK_MARKER = "FALL-THROUGH-WARN-TEST-FALLBACK-RAN"
_FALLBACK_EXIT = 17


def _make_stub_engine_root(tmp_path: Path) -> Path:
    """Same shape as `test_door_read_deadline.py::_make_stub_engine_root`:
    a throwaway, uniquely-stamped engine root so the derived pipe name/hash
    cannot collide with a real server, plus a fake `coordinator-invoke.py`
    that announces itself instead of running a real op."""
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


def _run_door(engine_root: Path, door_exe: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
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
    """Nothing is listening on the derived pipe, so the door degrades to
    cold ordinarily -- and that ordinary degrade must now be loud on
    stderr, never silent, and never on stdout."""
    root = _make_stub_engine_root(tmp_path)

    proc = _run_door(root, _default_named_door(tmp_path))

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


def test_warn_does_not_leak_onto_stdout(tmp_path: Path) -> None:
    """A stricter framing of the same property: the door's own warn text
    must never appear on stdout, which a caller may parse as data relayed
    from the dispatched CLI."""
    root = _make_stub_engine_root(tmp_path)

    proc = _run_door(root, _default_named_door(tmp_path))

    assert "door:" not in proc.stdout, (
        "the door's own diagnostic prefix must never appear on stdout -- "
        "stdout is reserved for the relayed CLI's own output"
    )
