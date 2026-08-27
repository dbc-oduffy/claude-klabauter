"""AC18 -- both the door's warm request AND its cold fallback resolve THIS
image's own name from ONE `GetModuleFileNameW`-based resolution, never
`argv[0]` and never the hardcoded `coordinator-invoke.py` literal this file
carried before C0.

WHAT REGRESSED, AND WHY THIS FILE EXISTS. `door.c :: fall_through` used to
build its cold command line as `{PYTHON_BIN_W} "<root>\\coordinator\\bin\\
coordinator-invoke.py"` unconditionally -- a hardcoded literal, not derived
from the running image's own basename. A door hardlinked under a different
name (e.g. `cross-repo-memo.exe`) that missed warm therefore ran
`coordinator-invoke.py list`: it did not fail, it MIS-DISPATCHED under a
different CLI's argument grammar, and if the relayed token happened to be
valid `coordinator-invoke` input it executed the wrong thing silently. Per
the dispatch brief, a renamed door's MISS is the common path (median server
lifetime 5.7 minutes), not a corner case.

THE FIX, and the two properties this file proves together:

  1. NAME-AWARE RESOLUTION. `fall_through` now targets `<root>\\coordinator\\
     bin\\<this image's own resolved basename>.py`, resolved from
     `GetModuleFileNameW` (via `resolve_own_basename`/
     `door_entrypoint_basename`), never `argv[0]` and never a second,
     independently-derived name.

  2. FAIL CLOSED. When no `coordinator/bin/<basename>.py` exists for the
     resolved name, the door refuses outright -- no process spawned at
     all -- with a diagnostic naming both the image and the missing script.
     It never substitutes `coordinator-invoke.py` for the missing script;
     that substitution is exactly the mis-dispatch this fix exists to kill.

  3. BACKWARD COMPATIBILITY. A door built/copied under the pre-C0 default
     name (`coordinator-invoke`) targets `coordinator-invoke.py` exactly as
     before -- this file's `test_pre_delivery_failure_still_falls_through`
     sibling (`test_door_read_deadline.py`) already covers that shape from
     the OTHER angle (env-var override, not a renamed copy); this file
     covers the RENAMED-image angle that regressed.

MECHANISM: `door.exe` is copied (never re-linked) to a second filename in a
throwaway `tmp_path`, exactly as an operator's install-time hardlink would
present a second name for the same image -- `GetModuleFileNameW` reads
whatever name the file was actually opened under, so a plain copy exercises
the same resolution a hardlink would. `COORDINATOR_DOOR_ENGINE_ROOT` points
both copies at a throwaway stub engine root (same shape
`test_door_read_deadline.py::_make_stub_engine_root` uses), so nothing here
touches a real published engine or the resident warm server.
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


def _make_stub_engine_root(tmp_path: Path) -> Path:
    """Same shape as `test_door_read_deadline.py::_make_stub_engine_root`:
    a throwaway `coordinator_core/_engine_stamp` (what `is_valid_engine_root_w`
    checks) so the door accepts this as a real engine root, unique per test
    run so the derived pipe name/hash cannot collide with a real server."""
    root = tmp_path / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"fall-through-name-aware-test-{os.getpid()}-{time.time_ns()}\n",
        encoding="utf-8",
    )
    (root / "coordinator" / "bin").mkdir(parents=True)
    return root.resolve()


def _run_named_door(exe_path: Path, engine_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    return subprocess.run(
        [str(exe_path), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(engine_root),
        creationflags=_NO_CONSOLE,
    )


def test_renamed_door_with_no_matching_script_fails_closed(tmp_path: Path) -> None:
    """A door copied to a name with no matching `coordinator/bin/<name>.py`
    must refuse outright -- no process spawned, a diagnostic naming both the
    image and the missing script -- never silently substitute
    `coordinator-invoke.py`'s grammar for it."""
    root = _make_stub_engine_root(tmp_path)
    renamed = tmp_path / "cross-repo-memo.exe"
    shutil.copyfile(_DOOR_EXE, renamed)

    proc = _run_named_door(renamed, root, "list")

    assert proc.returncode != 0
    assert "cross-repo-memo" in proc.stderr
    assert "coordinator-invoke.py" not in proc.stderr, (
        "a fail-closed refusal must name the ACTUAL missing script "
        "(cross-repo-memo.py), never substitute the default CLI's name"
    )
    assert proc.stdout == ""


def test_renamed_door_falls_through_to_its_own_matching_script(tmp_path: Path) -> None:
    """Once `coordinator/bin/<name>.py` DOES exist for the resolved name, the
    renamed door's cold fallback runs THAT script -- proving the resolution
    is genuinely name-driven, not merely a refusal that always fires."""
    root = _make_stub_engine_root(tmp_path)
    marker = "CROSS-REPO-MEMO-FALLBACK-RAN"
    (root / "coordinator" / "bin" / "cross-repo-memo.py").write_text(
        "import sys\n"
        f"print({marker!r}, sys.argv[1:])\n"
        "raise SystemExit(9)\n",
        encoding="utf-8",
    )
    renamed = tmp_path / "cross-repo-memo.exe"
    shutil.copyfile(_DOOR_EXE, renamed)

    proc = _run_named_door(renamed, root, "list")

    assert proc.returncode == 9
    assert marker in proc.stdout
    assert "['list']" in proc.stdout


def test_default_named_door_still_targets_coordinator_invoke(tmp_path: Path) -> None:
    """A door built/copied under the pre-C0 default name must still target
    `coordinator-invoke.py` -- BACKWARD COMPATIBILITY IS AN AC. Proven from
    the renamed-copy angle (a plain copy to the default name, rather than
    the env-var-only fixture `test_door_read_deadline.py` already covers),
    so this file's own mechanism is validated symmetrically against both
    outcomes rather than only the new one."""
    root = _make_stub_engine_root(tmp_path)
    marker = "DEFAULT-NAME-FALLBACK-RAN"
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(
        "import sys\n"
        f"print({marker!r}, sys.argv[1:])\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    default_named = tmp_path / "coordinator-invoke.exe"
    shutil.copyfile(_DOOR_EXE, default_named)

    proc = _run_named_door(default_named, root, "ping")

    assert proc.returncode == 3
    assert marker in proc.stdout
    assert "['ping']" in proc.stdout
