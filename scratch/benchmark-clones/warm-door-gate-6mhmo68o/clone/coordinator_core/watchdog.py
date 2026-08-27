"""Portable timeout and cooperative loop watchdog primitives.

Two primitives:

    cs_timeout(secs, cmd, ...) -> int
        Runs `cmd` with a wall-clock cap of `secs` seconds. Returns 124 on
        timeout (mirrors GNU timeout's exit-code contract, same as the bash
        oracle's Branch A/B), or the command's own exit code on normal exit.
        Python's `subprocess.run(..., timeout=...)` natively provides the
        wall-clock cap — there is no Branch A (system `timeout`/`gtimeout`
        binary) vs Branch B (background-kill fallback) split here; that split
        was a bash-only portability workaround for the absence of a native
        timeout primitive, and does not apply to Python.

    Watchdog
        Cooperative per-iteration checker. `.check(...)` is intended to be
        called at the TOP of a long-running loop; `.reset()` clears state so a
        fresh cycle can begin. Instance state stands in for the bash oracle's
        module-level globals (_CW_START_EPOCH / _CW_LAST_PROBE /
        _CW_STALL_COUNTER / _CW_INITIALIZED) — one Watchdog instance per
        logical watchdog cycle, matching the bash oracle's single-instance
        (non-reentrant) design.

NEGATIVE-SPECS (hard, carried over from the bash oracle):
  - D-state (uninterruptible I/O) processes cannot be force-killed by the OS;
    cs_timeout guarantees "bail and return control", not "guaranteed
    terminate" — same limitation as the bash oracle, inherent to any
    SIGKILL/TerminateProcess-based timeout mechanism, not specific to this
    port.
  - False-stall tradeoff: if the probe command's output granularity is
    coarser than the operation's actual progress, stall detection can fire on
    a legitimately-progressing loop. Tune `stall_count`/`interval_secs` or use
    a finer-grained probe.

DELIBERATE DIVERGENCE from the bash oracle (addendum rule 2, P1
unbounded-hang class): the bash oracle's `cs_watchdog_check` probe invocation
(`probe_out=$("$@" 2>/dev/null)`) has NO timeout on the probe subprocess
itself — a hung probe command hangs the watchdog that is supposed to bound
runaway loops, which is the exact defect class the porting addendum's rule 2
prohibits shipping. This port bounds the probe call to `interval_secs`
seconds (the caller's own declared polling cadence) and treats a probe
timeout the same as a probe failure (warn, do not increment stall counter,
ceiling check still applies) rather than reproducing the hang faithfully.
This is a bug fix, not a silent scope-drop — it adds a safety bound the
oracle lacked without removing the ceiling/stall/probe-failure return-code
contract.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Sequence

# Windows console-flash suppression (DR-054) — no-op on non-Windows.
_CREATIONFLAGS = 0
if sys.platform == "win32":
    _CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def cs_timeout(
    secs: float,
    cmd: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> int:
    """Run ``cmd`` with a wall-clock cap of ``secs`` seconds.

    Returns 124 on timeout (mirrors GNU timeout's exit-code contract), or the
    command's own exit code on normal completion. Returns 1 if ``cmd`` is
    empty (mirrors the bash oracle's "no command given after --" guard).

    stdin is always DEVNULL (rule 2 stdin guard — the wrapped command must
    not be able to block waiting on the caller's stdin).

    NEGATIVE-SPEC: the ``subprocess.run`` call below is a deliberate
    isolation boundary and must NEVER be converted to an in-process call.
    The whole point of this function is a hard wall-clock timeout with
    killability — an in-process call cannot be killed on timeout the way a
    child process can (``TimeoutExpired`` triggers a real kill/
    TerminateProcess). Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (mechanism: "hard timeout / killability").
    """
    if not cmd:
        print("cs_timeout: no command given after --", file=sys.stderr)
        return 1

    try:
        result = subprocess.run(
            list(cmd),
            timeout=secs,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            creationflags=_CREATIONFLAGS,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        # subprocess.run() already sent kill()/TerminateProcess() to the
        # child on timeout before raising — matches the bash oracle's
        # SIGTERM-then-SIGKILL Branch B behavior in spirit (a hard kill, not
        # graceful), modulo the D-state caveat above (neither implementation
        # can force-terminate an uninterruptible-I/O process).
        return 124
    except FileNotFoundError as exc:
        print(f"cs_timeout: command not found: {exc}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"cs_timeout: failed to launch command: {exc}", file=sys.stderr)
        return 126


@dataclass
class _WatchdogState:
    start_epoch: float = 0.0
    last_probe: Optional[str] = None
    stall_counter: int = 0
    initialized: int = 0


class Watchdog:
    """Cooperative per-iteration watchdog. One instance per logical cycle.

    Mirrors the bash oracle's module-level-globals-as-single-instance-state
    design (`_CW_START_EPOCH`/`_CW_LAST_PROBE`/`_CW_STALL_COUNTER`/
    `_CW_INITIALIZED`); construct a fresh instance (or call `.reset()`)
    between distinct long-running-loop phases.
    """

    def __init__(self) -> None:
        self._state = _WatchdogState()

    def reset(self) -> None:
        """Clear all watchdog state so a fresh check() cycle can begin."""
        self._state = _WatchdogState()

    @staticmethod
    def _now_epoch() -> float:
        """Seconds since epoch. Degrades to 0.0 with a stderr warning on
        clock failure, mirroring the bash oracle's `_cw_now_epoch` fail-soft
        behavior (`date +%s` failure -> warn + `echo 0`, ceiling disabled
        rather than crashing the caller's loop). `time.time()` is not
        documented to raise, but CPython does not guarantee it never will
        (e.g. an unreadable system clock can surface as OSError on some
        platforms) — this try/except keeps the same degrade contract for
        that rare case instead of letting the whole watchdog check crash.
        Review: code-reviewer F4 — prior "never raises" docstring overclaimed
        parity with the oracle's explicit fail-soft handling.
        """
        try:
            return time.time()
        except OSError as exc:
            print(
                f"cs_watchdog: WARNING — time.time() failed ({exc}); "
                "elapsed-time ceiling disabled",
                file=sys.stderr,
            )
            return 0.0

    def check(
        self,
        ceiling_secs: float,
        stall_count: int,
        interval_secs: float,
        probe_cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
    ) -> int:
        """Cooperative per-iteration watchdog check. Call at the TOP of each
        loop body.

        Returns:
            0  — continue the loop (within ceiling, probe progressing)
            1  — bail: ceiling exceeded
            2  — bail: stall detected (probe unchanged for stall_count
                 iterations)

        Probe-failure handling (including probe timeout — see module
        docstring's DELIBERATE DIVERGENCE note): the stall counter is NOT
        incremented and this call returns 0 (the ceiling check has already
        run above and would have returned 1 if exceeded). A warning is
        always emitted to stderr — never a silent bail-avoidance.

        NEGATIVE-SPEC: the ``subprocess.run`` call below (the probe
        invocation) is a deliberate isolation boundary and must NEVER be
        converted to an in-process call. Same property as ``cs_timeout``:
        the probe must be boundable and killable on its own
        ``interval_secs`` timeout without hanging the watchdog it is
        supposed to bound — an in-process call cannot be killed on
        timeout. Reason recorded in
        state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
        (mechanism: "hard timeout / killability").
        """
        if not probe_cmd:
            print("cs_watchdog_check: no probe_cmd given", file=sys.stderr)
            return 1

        now = self._now_epoch()
        st = self._state

        if st.initialized == 0:
            st.start_epoch = now
            st.last_probe = None
            st.stall_counter = 0
            st.initialized = 1

        elapsed = now - st.start_epoch
        if elapsed >= ceiling_secs:
            print(
                f"cs_watchdog_check: ceiling exceeded "
                f"({elapsed:.0f}s elapsed >= {ceiling_secs:.0f}s limit)",
                file=sys.stderr,
            )
            return 1

        try:
            proc = subprocess.run(
                list(probe_cmd),
                capture_output=True,
                text=True,
                timeout=interval_secs,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                creationflags=_CREATIONFLAGS,
            )
            probe_out = proc.stdout
            probe_rc = proc.returncode
        except subprocess.TimeoutExpired:
            print(
                "cs_watchdog_check: probe command timed out after "
                f"{interval_secs:.0f}s (no signal) — stall counter unchanged",
                file=sys.stderr,
            )
            return 0
        except OSError as exc:
            print(
                f"cs_watchdog_check: probe command failed to launch: {exc} "
                "(no signal) — stall counter unchanged",
                file=sys.stderr,
            )
            return 0

        if probe_rc != 0:
            print(
                f"cs_watchdog_check: probe command exited {probe_rc} "
                "(no signal) — stall counter unchanged",
                file=sys.stderr,
            )
            return 0

        if probe_out == st.last_probe and st.initialized > 1:
            st.stall_counter += 1
            if st.stall_counter >= stall_count:
                print(
                    "cs_watchdog_check: stall detected "
                    f"({st.stall_counter} consecutive identical probe outputs)",
                    file=sys.stderr,
                )
                return 2
        else:
            st.stall_counter = 0

        st.last_probe = probe_out
        st.initialized += 1

        return 0
