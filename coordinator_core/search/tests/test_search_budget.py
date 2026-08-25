"""coordinator_core.search.tests.test_search_budget -- C12: the hot-path search
budget is measured against process time, not wall clock, and is 500ms
(DR-344's brightline), not the previous 2.5s wall-clock number.

Two defects this file pins:

  1. The instrument. `time.process_time()` (this process's own user+system CPU
     time) replaces `time.perf_counter()` (wall clock). A test that SLEEPS but
     does no CPU work must NOT trip the cap -- that is the regression that
     would catch a silent revert back to wall clock, since a wall-clock timer
     trips on sleep and a process-time timer does not.
  2. The disposition. Past-budget is a DECLINE (`Unanswerable`, `answer()`
     returns None and the real command runs), never a truncate-and-disclose.
     See `engine.py`'s `budget_exhausted` docstring-comment for why: a search
     already this expensive in-process is exactly the shape real grep -- a
     separate process, off this hook's own budget -- should run instead.
"""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.search import engine
from coordinator_core.search.engine import GrepSource, SearchSpec, Unanswerable, run

pytestmark = pytest.mark.cadence


def test_budget_is_process_time_not_wall_clock(tmp_path, monkeypatch):
    """A search whose SLEEP dominates wall clock but consumes ~no CPU must not
    trip the budget -- pins the instrument against a silent revert to
    `time.perf_counter()` (wall clock), which the docstring-comment above
    `MAX_PROCESS_SECONDS` names as measuring peer load, not this call's cost.
    """
    target = tmp_path / "f.txt"
    target.write_text("alpha\n" * 5)

    # Budget tiny enough that any real wall-clock sleep would trip it, if the
    # instrument were wall clock.
    monkeypatch.setattr(engine, "MAX_PROCESS_SECONDS", 0.05)

    real_process_time = time.process_time
    calls = {"n": 0}

    def sleepy_process_time():
        calls["n"] += 1
        if calls["n"] == 2:
            # Sleep happens BETWEEN the start-of-run() sample and the
            # budget_exhausted() check inside scan() -- wall clock elapses,
            # process time barely does.
            time.sleep(0.3)
        return real_process_time()

    monkeypatch.setattr(engine.time, "process_time", sleepy_process_time)

    spec = SearchSpec(pattern="alpha", targets=[str(target)])
    result = run(spec, cwd=str(tmp_path))

    assert result.truncated is False
    assert result.cap_hit is None
    assert any("alpha" in line for line in result.lines)


def test_budget_exceeded_declines_not_truncates(tmp_path, monkeypatch):
    """Past-budget raises Unanswerable (decline) -- never sets truncated/cap_hit
    (truncate-and-disclose). `GrepSource.execute` (the only caller `answer()`
    goes through) must let that propagate so `answer()` returns None and the
    real grep runs, rather than serving a confidently partial result.
    """
    target = tmp_path / "f.txt"
    target.write_text("alpha\n" * 50)

    monkeypatch.setattr(engine, "MAX_PROCESS_SECONDS", -1.0)

    spec = SearchSpec(pattern="alpha", targets=[str(target)])
    with pytest.raises(Unanswerable):
        run(spec, cwd=str(tmp_path))

    source = GrepSource(spec=spec)
    with pytest.raises(Unanswerable):
        source.execute(cwd=str(tmp_path), stop_after=None)


def test_budget_constant_is_dr344_brightline():
    """500ms, not the previous 2.5s wall-clock figure -- DR-344's own bar."""
    assert engine.MAX_PROCESS_SECONDS == 0.5
