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


def test_a_pidless_marker_past_the_start_grace_is_an_unstarted_child_and_launches(tmp_path):
    _marker(tmp_path, "", cov.INFLIGHT_START_GRACE_S + 5)

    assert len(_resend(tmp_path)) == 1


def test_a_pidless_marker_inside_the_start_grace_still_parks_without_spawning(tmp_path):
    _marker(tmp_path, "", 1.0)

    assert _resend(tmp_path) == []


def test_a_marker_whose_pid_is_dead_is_replaced_and_launches(tmp_path):
    _marker(tmp_path, str(_dead_pid()), 1.0)

    assert len(_resend(tmp_path)) == 1


def test_a_marker_whose_pid_is_alive_parks_without_spawning_even_past_the_start_grace(tmp_path):
    _marker(tmp_path, str(os.getpid()), cov.INFLIGHT_START_GRACE_S + 5)

    assert _resend(tmp_path) == []


def test_a_live_pid_marker_older_than_the_ttl_is_replaced(tmp_path):
    _marker(tmp_path, str(os.getpid()), cov.INFLIGHT_TTL_S + 5)

    assert len(_resend(tmp_path)) == 1


def test_a_garbled_marker_is_read_as_pidless(tmp_path):
    _marker(tmp_path, "not-a-pid", cov.INFLIGHT_START_GRACE_S + 5)

    assert len(_resend(tmp_path)) == 1


def test_the_child_records_its_pid_only_into_a_marker_that_still_exists(tmp_path):
    cov._record_child_pid(tmp_path, "run1")
    assert not cov._inflight_path(tmp_path, "run1").exists()

    assert cov._claim_inflight(tmp_path, "run1") is True
    cov._record_child_pid(tmp_path, "run1")

    assert cov._inflight_path(tmp_path, "run1").read_text(encoding="ascii") == str(os.getpid())
    assert cov._claim_inflight(tmp_path, "run1") is False


def test_the_step_budgets_are_not_raised():
    assert (cov.REVERIFY_STEP_SECS, cov.REVERIFY_RESUME_DELAY_SECS) == (8.0, 30.0)
    assert cov.INFLIGHT_START_GRACE_S < cov.REVERIFY_RESUME_DELAY_SECS
