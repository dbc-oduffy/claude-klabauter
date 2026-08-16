from __future__ import annotations
"""
test_workday_complete_backfill_dispatch_timeout.py — regression test for
workday-complete-close.py's cmd_backfill_dispatch_rows / _dispatch_step9_row.

Spec backlink: state/audits/2026-08-15-fleet-composed-op-spawn-census.md row
14 / state/audits/2026-08-15-fleet-census-reverification-at-head.md.

Defect (measured): `_dispatch_step9_row` (the per-gap-date fan-out unit
`cmd_backfill_dispatch_rows` calls once per row, oldest-first, on a 14-day
lookback) invoked the entire composed step9-append-changelog ceremony via
`subprocess.run` with NO `timeout=` at all -- on this repo's 50-70-
concurrent-session load norm an unbounded spawn here can wedge a ceremony
with no session available to fix it.

Fix: `_dispatch_step9_row` now passes `timeout=_STEP9_ROW_DISPATCH_TIMEOUT_
SECS`, catching `subprocess.TimeoutExpired` and converting it into a
per-row failure (rc=1) rather than letting it propagate and abort the whole
backfill loop -- preserving `cmd_backfill_dispatch_rows`' own documented
per-row-isolation contract ("one bad day should not abandon the rest").

The per-row SPAWN SHAPE (one full step9 dispatch per gap date) is
deliberately left unchanged/unbatched: each row's exit code is tracked
independently in the loop, and batching multiple gap dates into one
invocation would turn N independent failures into one all-or-nothing
failure, which is not equivalent. This test asserts the invariants that
actually matter: (a) every row dispatch carries a timeout=, (b) a
duplicate date in the stdin blob dispatches step9 exactly once (2026-08-15
staff review: `processed_dates` was tracked but never gated the loop, so a
duplicated date double-appended/double-pushed that day's changelog), and
(c) a timeout on one row converts to a tracked per-row failure without
aborting subsequent rows. A prior manifest `spawn_count_budget` here
(`spawns_per_gap_date=1`) was removed as tautological -- N spawns for N
rows is true by definition for any per-row implementation and bounds
nothing; see budget-manifest.json's `bin.workday_complete_close_backfill_
dispatch_rows` rationale.
"""

import importlib.util
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin

_WORKDAY_CLOSE_CLI = os.path.join(_BIN_DIR, "workday-complete-close.py")


def _load_module():
    if _BIN_DIR not in sys.path:
        sys.path.insert(0, _BIN_DIR)
    loader = SourceFileLoader("workday_complete_close_module_spawn_test", _WORKDAY_CLOSE_CLI)
    spec = importlib.util.spec_from_file_location(
        "workday_complete_close_module_spawn_test", _WORKDAY_CLOSE_CLI, loader=loader
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Args:
    def __init__(self, **kw):
        self.for_date = None
        self.only_mode = False
        self.scope_summary = None
        self.no_push = False
        self.dry_run = False
        for k, v in kw.items():
            setattr(self, k, v)


def _fake_stdin_rows(n: int) -> str:
    lines = []
    for i in range(n):
        lines.append(f"2026-08-{10 + i:02d}\t1\tbaseSHA{i}\ttipSHA{i}")
    return "\n".join(lines) + "\n"


def test_backfill_dispatch_rows_every_dispatch_carries_a_timeout(monkeypatch) -> None:
    """Every row dispatch must pass timeout= through to subprocess.run --
    the previously-unbounded call site (state/audits/2026-08-15-fleet-
    composed-op-spawn-census.md row 14) is the defect this guards."""
    module = _load_module()

    calls = {"n": 0}

    class _Fake:
        returncode = 0

    def _fake_run(cmd, *a, **kw):
        calls["n"] += 1
        assert "timeout" in kw, "each row dispatch must carry a timeout="
        return _Fake()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    monkeypatch.setattr(module.sys, "stdin", _StdinStub(_fake_stdin_rows(3)))

    args = _Args()
    rc = module.cmd_backfill_dispatch_rows(args)

    assert calls["n"] == 3, "3 distinct gap-date rows must dispatch 3 times"
    assert rc == 0


def test_backfill_dispatch_rows_duplicate_date_dispatches_once(monkeypatch) -> None:
    """A gap-rows blob carrying the same date twice must dispatch step9 for
    that date exactly ONCE -- the stdin blob is produced upstream and never
    uniqued, so an un-gated loop double-appends/double-pushes that day's
    changelog. `processed_dates` gates the loop, not just an informational
    message."""
    module = _load_module()

    calls: list[str] = []

    class _Fake:
        returncode = 0

    def _fake_run(cmd, *a, **kw):
        for_date = cmd[cmd.index("--for-date") + 1] if "--for-date" in cmd else None
        calls.append(for_date)
        return _Fake()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    dup_rows = _fake_stdin_rows(2) + "2026-08-10\t1\tbaseSHA0\ttipSHA0\n"
    monkeypatch.setattr(module.sys, "stdin", _StdinStub(dup_rows))

    args = _Args()
    rc = module.cmd_backfill_dispatch_rows(args)

    assert calls == ["2026-08-10", "2026-08-11"], (
        f"duplicate 2026-08-10 row must dispatch once, not twice: {calls!r}"
    )
    assert rc == 0


def test_one_row_timeout_does_not_abort_the_rest_of_the_backfill(monkeypatch) -> None:
    """A subprocess.TimeoutExpired on one row must be tracked as that row's
    failure only -- subsequent rows still dispatch, and the loop returns a
    non-zero overall rc rather than raising."""
    module = _load_module()

    calls = {"n": 0}

    class _Fake:
        returncode = 0

    def _fake_run(cmd, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 0))
        return _Fake()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    monkeypatch.setattr(module.sys, "stdin", _StdinStub(_fake_stdin_rows(3)))

    args = _Args()
    rc = module.cmd_backfill_dispatch_rows(args)

    assert calls["n"] == 3, "a timeout on row 1 must not prevent rows 2 and 3 from dispatching"
    assert rc == 1, "a timed-out row must surface as an overall non-zero rc"


class _StdinStub:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text
