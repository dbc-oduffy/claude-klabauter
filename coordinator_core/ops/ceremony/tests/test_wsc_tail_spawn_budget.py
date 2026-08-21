"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_spawn_budget

C3 (docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md): gives
`ceremony.wsc_tail` -- the plan's own worked example of an op that spawns and
was never enrolled -- an END-TO-END, whole-op `op_total_*`-style spawn-count
pin, following `test_commit_e2e_spawn_budget.py`'s shape (that module's own
`_count_op_spawns_both_ways`/`_assert_op_total` precedent for
`ceremony.scoped_git_commit`).

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: see
`test_commit_e2e_spawn_budget.py`'s module docstring -- unchanged rationale,
not restated here. `ceremony.wsc_tail` itself already carries a regression-
tested latency budget (`test_wsc_tail_parity.py::
test_kpi_wsc_tail_blocking_path_under_2s`); this module is the spawn-count
axis that regression test does not cover.

WHY THIS PIN LIVES HERE, NOT IN `budget-manifest.json`. The precedent
(`ceremony.scoped_git_commit`) pins its `op_total_*` figures as manifest
`spawn_count_budget` keys, ratcheted by `test_spawn_count_budget_ratchet.py`
against the LIVE manifest. `budget-manifest.json` is out of this chunk's
declared `writes:` scope (C3's dispatch brief lists only this file and
`test_spawn_count_budget_ratchet.py`), so `ceremony.wsc_tail` cannot become a
manifest row here. `_WSC_TAIL_OP_TOTAL_CEILING` below is the same
ceiling+reason shape `_SPAWN_COUNT_HIGH_WATER` uses, kept local to this
module rather than merged into that table: the ratchet's own completeness
checks (`test_spawn_count_high_water_table_covers_every_override` /
`_stale_high_water_ops`) both key off the LIVE manifest, and an entry
naming an op absent from `overrides` would read as an orphaned mark and fail
`_stale_high_water_ops` -- the wrong failure for a row that is deliberately
not manifest-backed. A follow-up chunk that adds `ceremony.wsc_tail` to
`budget-manifest.json`'s `overrides` should migrate this local pin into
`_SPAWN_COUNT_HIGH_WATER` and delete it from here in the same commit.

COUNTING MECHANISM: the whole-op `subprocess.run` MODULE-ATTRIBUTE
substitution, the same "all_n" shape `test_commit_e2e_spawn_budget.py`'s
`_count_op_spawns_both_ways` uses for its `op_total_*` keys -- routing-
agnostic (catches a spawn anywhere in the op's reached set, not only calls
through one seam), which is what "whole-op" means here. `ceremony.wsc_tail`
composes `ceremony.scoped_git_commit`'s own `run_commit_pipeline` in-process
plus its own pre/post-commit tail steps (trailer derivation, sentinel
read/write, stamp+ship, origin-stub close, receipt emit), several of which
spawn `git` directly rather than through `scoped_git_commit`'s pipeline --
exactly the kind of reach a seam-scoped counter would miss.

MEASURED SHAPE (this session, `wsc_tail_repo` fixture -- a fresh single-file
commit on `main` with a configured, reachable `origin` and the default
deferred-push mode; `_spawn_deferred_push_skip_loud` monkeypatched to a
no-op exactly as `test_kpi_wsc_tail_blocking_path_under_2s` already does, so
this count never depends on a real detached child process):
  - 37 whole-op `subprocess.run` calls, all `git`. Matches this plan's own
    origin figure ("~37 spawns per close") for the first time as a REAL
    measurement rather than an inherited approximation -- see module
    docstring's own warning that the origin figure is stale by default.

This is an exact-equality assertion, deliberately, for the same reason
`test_commit_e2e_spawn_budget.py`'s pins are exact: the whole point of a
spawn-COUNT budget is that it does not drift quietly. A future change to
`wsc_tail._handler` (or anything it composes in-process) that adds or
removes a spawn must update this ceiling in the same commit, with a stated
reason, not silently pass.

Spec backlink: docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md § C3
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from .test_wsc_tail_parity import WscTailRepo, wsc_tail_repo, _unique_session_id  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


#: The whole-op `subprocess.run` ceiling for a normal `ceremony.wsc_tail`
#: pass -- see module docstring, "MEASURED SHAPE" above, for the fixture
#: shape this was measured against and why the pin lives here rather than
#: in `budget-manifest.json`. Shape: `(ceiling, reason)`, matching
#: `_SPAWN_COUNT_HIGH_WATER`'s per-mark convention in
#: `test_spawn_count_budget_ratchet.py` (a value may be LOWERED freely;
#: raising one requires editing this constant with a stated reason, turning
#: a silent drift into a visible diff).
_WSC_TAIL_OP_TOTAL_CEILING = 37
_WSC_TAIL_OP_TOTAL_REASON = (
    "First measured 2026-08-21 (C3, dlv-every-op-knows-what-it-spawns-f4bbf6): a fresh "
    "single-file commit on `main` through the full `ceremony.wsc_tail` handler, deferred-push "
    "mode with `_spawn_deferred_push_skip_loud` monkeypatched off. Matches the plan's own "
    "'~37 spawns per close' origin figure exactly -- the first time that number is a real "
    "measurement rather than an inherited approximation."
)


def _count_whole_op_spawns(fn) -> tuple[int, Any]:
    """Run *fn* once, substituting the `subprocess.run` MODULE ATTRIBUTE for
    the duration of the call, and return `(n, result)`.

    Mirrors `test_commit_e2e_spawn_budget.py::_count_op_spawns_both_ways`'s
    `all_n` half -- deliberately not the `git_native._git` seam half, since
    this module only asserts the whole-op `op_total_*`-style figure (see
    module docstring for why `ceremony.wsc_tail` needs the routing-agnostic
    counter, not a seam-scoped one).

    SCOPED TO `fn` ON PURPOSE, same rationale as the precedent: a counter
    installed for the whole test would also catch the fixture's own
    repo-building git calls, making the figure move whenever the fixture
    changed rather than when the op's own spawn shape changed.
    """
    allc = {"n": 0}
    orig_run = subprocess.run

    def _run_wrapper(*a, **kw):
        allc["n"] += 1
        return orig_run(*a, **kw)

    subprocess.run = _run_wrapper
    try:
        result = fn()
    finally:
        subprocess.run = orig_run
    return allc["n"], result


def test_wsc_tail_op_total_spawn_count_matches_pin(wsc_tail_repo, monkeypatch):
    """C3: a normal `ceremony.wsc_tail` pass -- a fresh single-file commit,
    deferred-push mode, no claim conflict -- spawns exactly
    `_WSC_TAIL_OP_TOTAL_CEILING` whole-op subprocesses, end to end.

    "Normal run" per the plan's own AC: a real commit lands (`committed_sha`
    is not None); `exit_code` may be 0 or 2 (`WSC_TAIL_EXIT_LADDER`'s "commit
    landed, tail item needs attention -- not a halt, proceed" -- see
    `coordinator_core/ceremony_common/cli_rejection.py`), never a failure
    that means nothing ran.
    """
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    def _run():
        return asyncio.run(
            wsc_tail_mod._handler(
                {
                    "sid": sid,
                    "subject": "workstream-complete: feature",
                    "stage_paths": ["tasks/feature/todo.md"],
                    "caller_paths": ["tasks/feature/todo.md"],
                },
                repo_root=repo.common_dir,
            )
        )

    n, result = _count_whole_op_spawns(_run)

    assert result["committed_sha"] is not None, (
        "fixture did not land a commit: %r -- this test measures the whole-op spawn count of "
        "a NORMAL close, not a refusal path" % (result,)
    )
    assert result["exit_code"] in (0, 2), (
        "unexpected exit_code %r -- WSC_TAIL_EXIT_LADDER only expects 0 (clean) or 2 (commit "
        "landed, tail item needs attention) on a normal pass: %r" % (result.get("exit_code"), result)
    )

    assert n == _WSC_TAIL_OP_TOTAL_CEILING, (
        "ceremony.wsc_tail whole-op spawn count is %d, pinned ceiling is %d (%s). Over budget is "
        "a kill candidate per docs/wiki/cost-budgets-and-the-kill-disposition.md, not a budget "
        "raise -- update _WSC_TAIL_OP_TOTAL_CEILING (and its reason) together with this test if "
        "the change is intentional." % (n, _WSC_TAIL_OP_TOTAL_CEILING, _WSC_TAIL_OP_TOTAL_REASON)
    )
