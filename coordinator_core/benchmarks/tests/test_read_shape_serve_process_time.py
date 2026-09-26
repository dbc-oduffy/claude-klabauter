"""coordinator_core.benchmarks.tests.test_read_shape_serve_process_time -- AC9's
process-time evidence for the read-shape serve path (`search/answer.py`'s
`ReadSource`, C1/C3).

Purpose: name and gate, in PROCESS TIME (never wall clock -- CLAUDE.md's own
Load norm: wall clock measures peer load on this box, not this code), the two
costs C9's dispatch brief calls "newly added and always-paid":

  1. the parse-then-decline cost `answer()`/`plan_for()` pay on a read-shaped
     command that ultimately does NOT qualify -- it still does recognition and
     parsing work, then falls through to the rest of the guard chain unchanged
     (`test_declining_read_shape_process_time_corpus` below);
  2. the serve cost itself, against the fork it replaces, including the
     stat-gate pre-check (`sources_read._read_text_strict`) that declines
     above the render cap without ever opening the file
     (`test_serve_cost_stays_far_under_the_fork_it_replaces_at_the_render_cap`,
     `test_decline_above_the_render_cap_is_flat_regardless_of_how_far_over`
     below).

NOT measured here, and named why (C9 dispatch brief, point 1): the
recognition-classification delta (`plan_for` calling `_classify`). `_classify`
already runs unconditionally for every Bash call before `plan_for` ever checks
`READ_VERBS`/`GREP_FAMILY` (`answer.py :: plan_for`), so that cost is paid
today regardless of this plan; the read branch adds only a `basename` call and
a set-membership check on top of a classification that already ran. Measuring
that delta would measure a number that is near zero by construction.

Method, both legs: `coordinator_core.benchmarks.process_time.in_process_time_ms`,
the shared batch-amortised in-process primitive (C6, docs/plans/2026-09-11-
perf-ratchets-measure-process-time-not-t.md) -- no subprocess is needed to
time Python code timing itself. The fork-side comparison
(`test_serve_cost_stays_far_under_the_fork_it_replaces_at_the_render_cap`)
additionally spawns a REAL `cat` through the resolved POSIX shell via
`coordinator_core.benchmarks.process_time.batched_process_time_ms` -- the same
K-batched job-object/kqueue primitive `test_bash_dispatch_process_time_gate.py`
uses for the dispatch entrypoint itself, reused here rather than a second
instrument (this file's own dispatch brief, and the plan's Cross-plan
coordination section, on why `benchmarks/` is the delivered home for this).

Named budgets, and why each number is what it is (not "a budget the existing
instrumentation already expresses" -- the dispatch brief's own words for the
answer this file must not give):

  - `_DECLINE_PATH_CEILING_MS = 10.0`. The measured PreToolUse chain floor is
    136.8ms process time (this plan's own Problem section, live on this box,
    `_arm_lazy_ops()` already called before dispatch, per that section's own
    correction). DR-344 forbids any single guard-chain process exceeding
    200ms, which leaves ~63ms of headroom before this plan added anything.
    10ms keeps a single declining call inside a sixth of that headroom --
    generous for what is at most a handful of string/flag comparisons plus,
    for the produce-time decline cases, one `os.stat()` call.
  - `_SERVE_AT_CAP_CEILING_MS = 20.0`. The fork this plan replaces is not the
    2.25s the source memo measured (98% wrapper, per the Problem section) --
    it is the ~48.7ms process-time floor of a bare, non-login
    `bash -c 'cat ...'` the same section's decomposition table names. Serving
    must beat that by a wide margin to be worth having; 20ms is under half of
    it, asserted even at the render cap's own byte ceiling
    (`engine.MAX_RENDER_BYTES`, 48000 bytes) where the in-process read+decode
    is at its most expensive.

Spec backlink: docs/plans/2026-08-22-a-bash-call-stops-costing-a-second-and-a-
half.md, chunk C9 (AC9).
"""

from __future__ import annotations

import os
from typing import Callable

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
    in_process_time_ms,
)
from coordinator_core.search.answer import answer
from coordinator_core.search.engine import MAX_RENDER_BYTES
from coordinator_core.search.tests._posix_shell import POSIX_SHELL, requires_posix_shell

K_SUBPROCESS = 20
"""Matches `bash_dispatch_probe.K_INVOCATIONS` -- the amortisation factor
`batched_process_time_ms`'s own module docstring calls for against the
~15.6ms Windows scheduler-tick quantisation."""

_DECLINE_PATH_CEILING_MS = 10.0
"""See module docstring -- ~1/6 of the ~63ms headroom the measured 136.8ms
chain floor leaves under DR-344's 200ms single-process bar."""

_SERVE_AT_CAP_CEILING_MS = 20.0
"""See module docstring -- under half of the ~48.7ms bare non-login
`bash -c 'cat ...'` fork floor this plan's Problem section measures, even at
the render cap's own byte ceiling."""


def _require_supported_platform() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            "process-time accounting has no primitive for this platform -- "
            "see coordinator_core.benchmarks.process_time module docstring"
        )


def _min_process_time_ms(fn: Callable[[], object]) -> float:
    return in_process_time_ms(fn)["process_time_ms"]


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


def _cmd_path(path) -> str:
    return str(path).replace("\\", "/")


@pytest.fixture()
def decline_corpus(tmp_path):
    """Read-shaped commands that ultimately decline, spanning both decline
    stages `answer()` can hit -- not just non-matching commands (a command
    whose first token is not in `READ_VERBS` at all costs nothing extra by
    construction and would prove nothing; see the C9 dispatch brief)."""
    small = tmp_path / "small.txt"
    _write(str(small), b"one\ntwo\nthree\n")
    oversized = tmp_path / "oversized.txt"
    _write(str(oversized), b"x" * (MAX_RENDER_BYTES + 1))
    other = tmp_path / "other.txt"
    _write(str(other), b"content\n")

    return {
        "unmodeled_flag": "cat -e %s" % _cmd_path(small),
        "multiple_operands_head": "head -n 5 %s %s" % (_cmd_path(small), _cmd_path(other)),
        "unsupported_sed_program": "sed -e 's/a/b/' %s" % _cmd_path(small),
        "stdin_operand": "cat -",
        "missing_file": "cat %s" % _cmd_path(tmp_path / "does-not-exist.txt"),
        "oversized_file": "cat %s" % _cmd_path(oversized),
        "glob_operand": "cat %s" % _cmd_path(tmp_path / "*.txt"),
    }


@pytest.mark.parametrize(
    "label",
    [
        "unmodeled_flag",
        "multiple_operands_head",
        "unsupported_sed_program",
        "stdin_operand",
        "missing_file",
        "oversized_file",
        "glob_operand",
    ],
)
def test_declining_read_shape_process_time_corpus(decline_corpus, tmp_path, label):
    cmd = decline_corpus[label]
    assert answer(cmd, cwd=str(tmp_path)) is None, (
        f"{label}: expected this row to decline (return None), it did not -- "
        "corpus row no longer measures a decline path"
    )
    best_ms = _min_process_time_ms(lambda: answer(cmd, cwd=str(tmp_path)))
    assert best_ms <= _DECLINE_PATH_CEILING_MS, (
        f"{label}: declining read-shape process time {best_ms:.3f}ms exceeds "
        f"the {_DECLINE_PATH_CEILING_MS}ms budget"
    )


@pytest.mark.parametrize(
    "size_bytes",
    [100, 2_000, 20_000, MAX_RENDER_BYTES - 1],
    ids=["100b", "2kb", "20kb", "at_cap_minus_1"],
)
def test_serve_cost_stays_under_ceiling_up_to_the_render_cap(tmp_path, size_bytes):
    path = tmp_path / "served.txt"
    _write(str(path), b"a" * size_bytes)
    cmd = "cat %s" % _cmd_path(path)

    assert answer(cmd, cwd=str(tmp_path)) is not None, (
        "expected this row to be served, it declined -- corpus row no "
        "longer measures the serve path"
    )
    best_ms = _min_process_time_ms(lambda: answer(cmd, cwd=str(tmp_path)))
    assert best_ms <= _SERVE_AT_CAP_CEILING_MS, (
        f"{size_bytes} bytes: serve process time {best_ms:.3f}ms exceeds "
        f"the {_SERVE_AT_CAP_CEILING_MS}ms budget"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
@requires_posix_shell
def test_serve_cost_stays_far_under_the_fork_it_replaces_at_the_render_cap(tmp_path):
    _require_supported_platform()
    path = tmp_path / "at_cap.txt"
    _write(str(path), b"a" * (MAX_RENDER_BYTES - 1))
    cmd = "cat %s" % _cmd_path(path)

    assert answer(cmd, cwd=str(tmp_path)) is not None
    serve_ms = _min_process_time_ms(lambda: answer(cmd, cwd=str(tmp_path)))

    fork_result = batched_process_time_ms(
        [POSIX_SHELL, "-c", cmd], k=K_SUBPROCESS, cwd=str(tmp_path)
    )
    fork_ms = fork_result["process_time_ms"]

    assert serve_ms < fork_ms, (
        f"at {MAX_RENDER_BYTES - 1} bytes (the render cap): in-process serve "
        f"({serve_ms:.3f}ms process time) is no longer cheaper than the real "
        f"fork it replaces ({fork_ms:.3f}ms, k={fork_result['k']}, "
        f"procs_per_call={fork_result['procs_per_call']}) -- a crossover file "
        "size now exists inside the render cap and AC9 requires naming it"
    )


def test_decline_above_the_render_cap_is_flat_regardless_of_how_far_over(tmp_path):
    just_over = tmp_path / "just_over.txt"
    _write(str(just_over), b"a" * (MAX_RENDER_BYTES + 1))
    far_over = tmp_path / "far_over.txt"
    _write(str(far_over), b"a" * (MAX_RENDER_BYTES * 100))

    cmd_just_over = "cat %s" % _cmd_path(just_over)
    cmd_far_over = "cat %s" % _cmd_path(far_over)
    assert answer(cmd_just_over, cwd=str(tmp_path)) is None
    assert answer(cmd_far_over, cwd=str(tmp_path)) is None

    just_over_ms = _min_process_time_ms(lambda: answer(cmd_just_over, cwd=str(tmp_path)))
    far_over_ms = _min_process_time_ms(lambda: answer(cmd_far_over, cwd=str(tmp_path)))

    assert just_over_ms <= _DECLINE_PATH_CEILING_MS
    assert far_over_ms <= _DECLINE_PATH_CEILING_MS, (
        f"a file 100x the render cap declined in {far_over_ms:.3f}ms, over "
        f"the {_DECLINE_PATH_CEILING_MS}ms budget -- the stat-gate is no "
        "longer O(1) in file size (it may be reading before stat-ing)"
    )
