"""
coordinator_core.orient_assemble.tests.test_readers_perform_no_disk_mutation
— C3 of docs/plans/2026-08-31-orient-assemble-stops-running-a-fleet-re.md.

Two guards, because the contract lie (`brief()` mutates fourteen sibling
repositories' `.git/hooks` as a side effect of orienting) and the budget
breach (1.9s process time against DR-344's 500ms brightline) are separable
defects — a fix for one does not detect the other:

  (1) NO-MUTATION. `brief()` at every cadence must perform zero disk writes,
      caught at the primitive that actually fired the mutation this plan
      exists to close: `git_hook_install._atomic_write`'s `open(mode="w")`.
      `test_read_only_guarantee.py`'s existing fixture patches
      `pathlib.Path.write_text`/`write_bytes` only — `_atomic_write` uses a
      bare `open()`, which that fixture never touched, so the mutation this
      plan closes could have fired underneath a green "read-only" suite the
      whole time. This file patches `builtins.open` for any write/append
      mode instead, so it catches a mutation FOUR call-frames down
      (`brief` -> `readers_health_reaper.collect` -> `_read_hook_currency`
      -> `cmd_hook_currency` -> `ensure_hooks_fleet` -> `_ensure_hook` ->
      `_atomic_write`) the way the original defect actually hid.

  (2) BUDGET. `_read_hook_currency` measured under a named process-time
      ceiling — process time, never wall clock (DR-344 § Load norm: wall
      measures the other ~50 sessions sharing the box, not this op).

      The plan's budget diagnosis was WRONG, and the C1/C2 execution proved
      it: after the check-only split landed, `_read_hook_currency` measured
      ~2.02s-2.09s process time -- slightly WORSE than the 1.906s the plan's
      Problem table recorded. Removing the write could never have helped,
      because the write was never the cost.

      The cost was a per-item spawn. `_read_hook_currency` over 19 registered
      repos issued 36 `machine-local get` subprocesses, 35 of them asking for
      `repos.claude_klabauter` -- the same key, the same answer, 35 times, at
      0.606s wall / 0.172s cpu each. Two call sites drove it
      (`_resolve_coord_bin` and `_resolve_claude_klabauter_bin_sh`), both reading a
      registry that cannot change between two repos of one fleet walk. That
      is the shape `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`
      exists to catch, reached through a non-git spawn.

      Fixed by memoizing `_ml_get` (see `_ML_GET_CACHE`'s comment in
      `coordinator/bin/lib/git_hook_install.py` for the staleness tradeoff,
      named rather than hidden). Measured after that fix, same box, same day:

          spawns        36     -> 2
          process time  1.906s -> 0.266s / 0.453s across repeat runs
          wall          232.9s -> 1.637s / 1.727s

      Process time is now inside DR-344's 500ms bar, which is why the ceiling
      below is a real budget rather than a rubber stamp. It is set at 1.0s,
      NOT 500ms: the honest measurement tops out at 0.453s, and pinning the
      assertion at 90% of the observed maximum buys flakiness on a box
      carrying ~50 concurrent sessions. The extra headroom is anti-flake, not
      permission to breach -- if this test starts failing at 1.0s, process
      time has roughly doubled and something reintroduced per-item work.
"""

from __future__ import annotations

import builtins
import io
import time
from contextlib import redirect_stderr

import pytest

from coordinator_core.orient_assemble import CADENCES, brief
from coordinator_core.orient_assemble import readers_health_reaper as rhr

#: Measured 2026-08-31 after the `_ml_get` memoization: 0.266s and 0.453s
#: process time across repeat runs of `_read_hook_currency()` alone, down
#: from 1.906s and 36 spawns. DR-344's bar is 500ms and the op now fits it.
#: The assertion sits at 1.0s rather than 500ms purely to survive box-load
#: variance at 90%-of-budget -- see the module docstring. Anti-flake, not
#: permission.
_HOOK_CURRENCY_PROCESS_TIME_CEILING_S = 1.0


@pytest.fixture
def forbid_any_disk_write(monkeypatch):
    """Patch `builtins.open` itself, not `Path.write_text`/`write_bytes` --
    `git_hook_install._atomic_write` (the actual mutation this plan closes)
    uses a bare `open(tmp, "w", ...)`, four call-frames below `brief()`,
    which the existing `Path`-level fixture in `test_read_only_guarantee.py`
    never reached. Any write/append-mode open anywhere beneath the call
    fails the test loudly."""
    real_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(
                f"disk write attempted via open(file={file!r}, mode={mode!r})"
            )
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)


def test_brief_performs_no_disk_write_at_any_cadence(forbid_any_disk_write):
    """AC(1): the read-only claim `orient_assemble.brief()`'s own docstring
    now makes is a fact this test enforces, not prose the next reader added
    has to remember to keep true."""
    for cadence in CADENCES:
        buf = io.StringIO()
        with redirect_stderr(buf):
            brief(cadence)


def test_read_hook_currency_performs_no_disk_write(forbid_any_disk_write):
    """Narrower repro of the actual defect this plan closes: calling the
    reader directly (not through the full `brief()` seam) must not write --
    the mutation lived here, in `_read_hook_currency` -> `cmd_hook_currency`
    (bare form) -> `ensure_hooks_fleet`, before C1/C2 threaded `check_only`
    through it."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        rhr._read_hook_currency()


def test_read_hook_currency_is_under_its_measured_process_time_ceiling():
    """AC(2): process time, never wall clock -- see module docstring for the
    measured baseline and why it is not the DR-344 500ms bar."""
    t0 = time.process_time()
    buf = io.StringIO()
    with redirect_stderr(buf):
        rhr._read_hook_currency()
    elapsed = time.process_time() - t0
    assert elapsed < _HOOK_CURRENCY_PROCESS_TIME_CEILING_S, (
        f"_read_hook_currency took {elapsed:.3f}s process time, over its "
        f"{_HOOK_CURRENCY_PROCESS_TIME_CEILING_S}s ceiling. The measured "
        "baseline is 0.266s-0.453s, so this means process time has roughly "
        "doubled -- look for reintroduced PER-ITEM work in the fleet walk "
        "before suspecting anything else. That is what cost 1.906s and 36 "
        "subprocess spawns here before `_ML_GET_CACHE` landed; the first "
        "fix attempt (removing the fleet WRITE) moved this number in the "
        "wrong direction, so do not reach for it again."
    )
