"""Tests for coordinator_core.watchdog — parity + edge-case coverage.

Port of: test-coordinator-watchdog.sh (coordinator-claude c6d97219, 2026-07-22) — mirrors
its AC set (timeout return-code 124, ceiling bail, stall bail, probe-failure
no-signal) plus the Python-port-specific edge cases (empty cmd, probe timeout
— see module docstring's DELIBERATE DIVERGENCE note).
"""

from __future__ import annotations

import sys
import time

import pytest

from coordinator_core.watchdog import Watchdog, cs_timeout


# ---------------------------------------------------------------------------
# cs_timeout
# ---------------------------------------------------------------------------


def test_cs_timeout_returns_command_exit_code_on_normal_completion():
    assert cs_timeout(5, [sys.executable, "-c", "import sys; sys.exit(0)"]) == 0
    assert cs_timeout(5, [sys.executable, "-c", "import sys; sys.exit(7)"]) == 7


def test_cs_timeout_returns_124_on_timeout():
    # AC parity with the bash oracle: GNU-timeout-style exit code 124.
    rc = cs_timeout(0.2, [sys.executable, "-c", "import time; time.sleep(5)"])
    assert rc == 124


def test_cs_timeout_empty_cmd_returns_1():
    assert cs_timeout(5, []) == 1


def test_cs_timeout_command_not_found_returns_127():
    assert cs_timeout(5, ["definitely-not-a-real-binary-xyz"]) == 127


def test_cs_timeout_does_not_block_on_stdin():
    # Regression for addendum rule 2 stdin guard: a child that reads stdin
    # must see EOF immediately (DEVNULL), not hang waiting for input.
    rc = cs_timeout(
        5,
        [sys.executable, "-c", "import sys; d = sys.stdin.read(); sys.exit(0 if d == '' else 1)"],
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# Watchdog.check — ceiling
# ---------------------------------------------------------------------------


def test_watchdog_ceiling_bails_with_1():
    wd = Watchdog()
    # First call initializes start_epoch to "now"; force elapsed >= ceiling
    # by using a ceiling of 0 seconds — every call after init is >= 0.
    rc1 = wd.check(0, 3, 1, [sys.executable, "-c", "print('a')"])
    # First call sets baseline THEN checks ceiling with elapsed==0 >= 0 -> bail
    assert rc1 == 1


def test_watchdog_continues_within_ceiling():
    wd = Watchdog()
    rc = wd.check(60, 3, 1, [sys.executable, "-c", "print('a')"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Watchdog.check — stall detection
# ---------------------------------------------------------------------------


def test_watchdog_stall_detected_after_n_identical_outputs():
    wd = Watchdog()
    probe = [sys.executable, "-c", "print('same')"]
    # First call: baseline (initialized 0 -> 1), never counts as stall.
    assert wd.check(60, 2, 1, probe) == 0
    # Second call: same output, initialized>1 now -> stall_counter=1 (< 2)
    assert wd.check(60, 2, 1, probe) == 0
    # Third call: same output again -> stall_counter=2 (>= 2) -> bail
    assert wd.check(60, 2, 1, probe) == 2


def test_watchdog_stall_counter_resets_on_changed_output():
    wd = Watchdog()
    same = [sys.executable, "-c", "print('same')"]
    diff = [sys.executable, "-c", "print('different')"]
    assert wd.check(60, 3, 1, same) == 0
    assert wd.check(60, 3, 1, same) == 0  # stall_counter=1
    assert wd.check(60, 3, 1, diff) == 0  # resets stall_counter=0
    assert wd.check(60, 3, 1, same) == 0  # stall_counter=1 again (not 3rd)


def test_watchdog_reset_clears_state():
    wd = Watchdog()
    probe = [sys.executable, "-c", "print('same')"]
    wd.check(60, 2, 1, probe)
    wd.check(60, 2, 1, probe)
    wd.reset()
    # Fresh cycle: first call after reset is baseline again, not a stall hit.
    assert wd.check(60, 2, 1, probe) == 0


# ---------------------------------------------------------------------------
# Watchdog.check — probe failure / no-signal handling
# ---------------------------------------------------------------------------


def test_watchdog_probe_nonzero_exit_does_not_increment_stall_or_bail():
    wd = Watchdog()
    failing_probe = [sys.executable, "-c", "import sys; sys.exit(3)"]
    rc = wd.check(60, 1, 1, failing_probe)
    assert rc == 0


def test_watchdog_probe_timeout_does_not_hang_and_does_not_bail():
    """Regression test for the P1 unbounded-hang class (addendum rule 2):
    the bash oracle's probe call has no timeout and can hang the watchdog
    that is supposed to bound runaway loops. This port bounds the probe to
    interval_secs and must return promptly, not hang."""
    wd = Watchdog()
    hanging_probe = [sys.executable, "-c", "import time; time.sleep(5)"]
    started = time.monotonic()
    rc = wd.check(60, 1, 0.2, hanging_probe)
    elapsed = time.monotonic() - started
    assert rc == 0
    assert elapsed < 3, "probe timeout must bound the call, not hang for the full sleep"


def test_watchdog_empty_probe_cmd_returns_1():
    wd = Watchdog()
    assert wd.check(60, 3, 1, []) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
