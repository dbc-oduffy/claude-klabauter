"""`reverify_on_mismatch`'s `overlay-inflight` marker proves a live child: a marker left by a child that
never started (no pid past the start grace, or a dead pid) is replaced and the overlay launches, while a
live child's marker (alive pid, or pidless inside the grace) still parks without a second spawn."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_deadline
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MEASURED = {"sha": "a" * 40, "base_sha": "b" * 40, "blobs": {"x": "1" * 40}}
_STALE = cov.OverlayVerdict("mismatch", "keyed to a different landing set blobs", ("t/x.py",))


def _marker(root: Path, text: str, age_s: float) -> Path:
    path = cov._inflight_path(root, "run1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path


def _resend(root: Path) -> list:
    spawned: list = []
    with pytest.raises(land_deadline.LandDeadlineExceeded):
        cov.reverify_on_mismatch(root, "run1", _MEASURED, _STALE, spawn=lambda *a: spawned.append(a) or True)
    return spawned


def _dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, **no_console_creationflags()
    )
    proc.wait()
    return proc.pid


def test_a_marker_whose_pid_is_dead_is_replaced_and_launches(tmp_path):
    _marker(tmp_path, str(_dead_pid()), 1.0)

    assert len(_resend(tmp_path)) == 1
