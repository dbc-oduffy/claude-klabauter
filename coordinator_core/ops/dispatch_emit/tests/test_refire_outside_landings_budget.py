"""The refire outside-credit walk runs under a caller read budget and degrades to uncredited on a stall."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_objects import PackReadDeadline
from coordinator_core.ops.dispatch_emit import plan_row_coding, sizing_fire

_ROWS = [{"id": "C1", "writes": ["a.py"]}]


@pytest.fixture(autouse=True)
def _head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.git.git_state.head_sha", lambda root: "a" * 40)
    monkeypatch.setattr("coordinator_core.git.git_dir.resolve_git_common_dir", lambda root: Path("."))


def _no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("retry_past_stall ran on the budgeted path")

    monkeypatch.setattr(git_objects, "retry_past_stall", boom)


def test_budget_scopes_the_walk_and_a_stall_degrades_to_uncredited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def outside(*_a, **_k):
        seen.append(git_objects._READ_BUDGET.get())
        raise PackReadDeadline("read budget exhausted")

    monkeypatch.setattr(plan_row_coding, "outside_landings", outside)
    _no_retry(monkeypatch)
    assert plan_row_coding.rows_landed_outside_executor(tmp_path, "plan.md", _ROWS, budget_secs=0.3) == {}
    assert len(seen) == 1 and seen[0] is not None
    assert git_objects._READ_BUDGET.get() is None


def test_budgeted_walk_that_finishes_credits_the_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_row_coding, "outside_landings", lambda *_a, **_k: {"C1": ["b" * 40]})
    _no_retry(monkeypatch)
    assert plan_row_coding.rows_landed_outside_executor(tmp_path, "plan.md", _ROWS, budget_secs=0.3) == {
        "C1": "b" * 40
    }


def test_without_a_budget_a_persisting_stall_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def outside(*_a, **_k):
        raise PackReadDeadline("stalled")

    monkeypatch.setattr(plan_row_coding, "outside_landings", outside)
    with pytest.raises(PackReadDeadline):
        plan_row_coding.rows_landed_outside_executor(tmp_path, "plan.md", _ROWS)


def test_the_budget_is_well_under_the_500ms_bar() -> None:
    assert 0 < sizing_fire.OUTSIDE_CREDIT_BUDGET_SECS < 0.5

