
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

    monkeypatch.setattr(engine, "MAX_PROCESS_SECONDS", 0.05)

    real_process_time = time.process_time
    calls = {"n": 0}

    def sleepy_process_time():
        calls["n"] += 1
        if calls["n"] == 2:
            time.sleep(0.3)
        return real_process_time()

    monkeypatch.setattr(engine.time, "process_time", sleepy_process_time)

    spec = SearchSpec(pattern="alpha", targets=[str(target)])
    result = run(spec, cwd=str(tmp_path))

    assert result.truncated is False
    assert result.cap_hit is None
    assert any("alpha" in line for line in result.lines)


def test_budget_exceeded_declines_not_truncates(tmp_path, monkeypatch):
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
    assert engine.MAX_PROCESS_SECONDS == 0.5
