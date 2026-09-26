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

  (3) SUBPROCESS TRAP (C3 of docs/plans/2026-09-11-the-orient-probes-run-
      without-an-em-read.md). `forbid_any_disk_write` patches `builtins.open`
      only, so it cannot see a subprocess-driven mutation (e.g.
      `git_maintenance.run_tier`'s gc/repack, which spawns `git` and performs
      no in-process write-mode `open()`). `forbid_any_subprocess_spawn` below
      monkeypatches `subprocess.run`/`Popen` to raise, applied alongside the
      open-guard across `brief()` at every cadence and against each of the
      four C1/C2 readers directly — the claim that this family mutates
      nothing is enforced here, not asserted in a docstring.

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
import subprocess
import time
from contextlib import redirect_stderr

import pytest

from coordinator_core.orient_assemble import CADENCES, brief
from coordinator_core.orient_assemble import readers_health_reaper as rhr

_HOOK_CURRENCY_PROCESS_TIME_CEILING_S = 1.0


@pytest.fixture
def forbid_any_disk_write(monkeypatch):
    real_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(
                f"disk write attempted via open(file={file!r}, mode={mode!r})"
            )
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)


@pytest.fixture
def forbid_any_subprocess_spawn(monkeypatch):
    """Monkeypatches `subprocess.run`/`Popen` to raise — the subprocess half
    of the no-mutation guard (see module docstring § (3) SUBPROCESS TRAP).
    `forbid_any_disk_write` alone cannot see a spawned git mutation
    (`git_maintenance.run_tier`'s gc/repack performs no in-process
    write-mode `open()`); this fixture closes that gap."""

    def _guarded_run(*args, **kwargs):
        raise AssertionError(f"subprocess.run attempted with args={args!r} kwargs={kwargs!r}")

    def _guarded_popen_init(self, *args, **kwargs):
        raise AssertionError(f"subprocess.Popen attempted with args={args!r} kwargs={kwargs!r}")

    monkeypatch.setattr(subprocess, "run", _guarded_run)
    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_popen_init)


def test_brief_performs_no_disk_write_at_any_cadence(forbid_any_disk_write):
    for cadence in CADENCES:
        buf = io.StringIO()
        with redirect_stderr(buf):
            brief(cadence)


def test_brief_performs_no_disk_write_or_subprocess_spawn_at_any_cadence(
    forbid_any_disk_write, forbid_any_subprocess_spawn
):
    for cadence in CADENCES:
        buf = io.StringIO()
        with redirect_stderr(buf):
            brief(cadence)


def test_read_plugin_drift_performs_no_disk_write_or_subprocess_spawn(
    forbid_any_disk_write, forbid_any_subprocess_spawn
):
    rhr._read_plugin_drift()


def test_read_git_maintenance_due_performs_no_disk_write_or_subprocess_spawn(
    forbid_any_disk_write, forbid_any_subprocess_spawn
):
    rhr._read_git_maintenance_due(".", "day")


def test_read_goal_coverage_performs_no_disk_write_or_subprocess_spawn(
    forbid_any_disk_write, forbid_any_subprocess_spawn
):
    rhr._read_goal_coverage()


def test_read_trail_scope_performs_no_disk_write_or_subprocess_spawn(
    forbid_any_disk_write, forbid_any_subprocess_spawn
):
    rhr._read_trail_scope()


def test_read_hook_currency_performs_no_disk_write(forbid_any_disk_write):
    buf = io.StringIO()
    with redirect_stderr(buf):
        rhr._read_hook_currency()


def test_read_hook_currency_is_under_its_measured_process_time_ceiling():
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
