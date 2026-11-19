"""land_byte_gate.admit admits an uncontended landing whatever its spent op budget, and a real park
names the budget left and the leg that spent it.

Driven through the default-argument `admit` inside a `land_deadline.scope` on a fake clock; the
contended side holds the gate from a second thread (a distinct context, so no re-entrancy).
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_byte_gate, land_deadline

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


class Fake:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Fake(1000.0)
    monkeypatch.setattr(land_deadline, "_now", fake)
    return fake


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), capture_output=True, check=True,
                   creationflags=_NO_WINDOW)
    return root


@pytest.fixture
def other_landing(repo: Path):
    """A second landing holding the repo's byte gate until the test releases it."""
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with land_byte_gate.admit(repo, 30, holder_label="other-landing", anchor_root=repo):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(10), "the second landing never took the gate"
    try:
        yield release
    finally:
        release.set()
        thread.join(10)


def _spend_budget(clock: Fake) -> None:
    land_deadline.mark("land_run.slow_leg")
    clock.now += land_deadline.LAND_BUDGET_SECS + 1.0


def test_a_free_gate_is_admitted_with_the_op_budget_already_spent(repo, clock):
    ran = []
    with land_deadline.scope():
        _spend_budget(clock)
        with land_byte_gate.admit(repo, anchor_root=repo) as admitted_at:
            ran.append(admitted_at)
    assert len(ran) == 1


def test_a_gate_held_by_another_landing_still_parks_with_no_budget_left(repo, clock, other_landing):
    with land_deadline.scope():
        _spend_budget(clock)
        with pytest.raises(land_deadline.LandDeadlineExceeded) as raised:
            with land_byte_gate.admit(repo, anchor_root=repo):
                pytest.fail("a contended gate with no budget left must park")
    exc = raised.value
    assert exc.leg == land_byte_gate.GATE_STEP and exc.step is True
    assert exc.left_secs == 0.0
    assert exc.bound_by == land_deadline.BOUND_BUDGET
    assert exc.spent_by == "land_run.slow_leg"


def test_the_parked_reply_names_the_budget_left_and_the_leg_that_spent_it(repo, clock, other_landing):
    with land_deadline.scope():
        _spend_budget(clock)
        with pytest.raises(land_deadline.LandDeadlineExceeded) as raised:
            with land_byte_gate.admit(repo, anchor_root=repo):
                pass
    fields = land_deadline.parked_fields(raised.value)
    assert fields["status"] == "not-started" and fields["parked"] is True
    assert fields["step"] == land_byte_gate.GATE_STEP
    assert fields["left_secs"] == 0.0 and fields["bound_by"] == land_deadline.BOUND_BUDGET
    assert fields["spent_by"] == "land_run.slow_leg"
    assert "budget left at entry 0.00s" in fields["error"] and "spent by land_run.slow_leg" in fields["error"]


def test_a_queue_past_the_gate_step_cap_parks_naming_the_step_bound(repo, other_landing, monkeypatch):
    monkeypatch.setattr(land_byte_gate, "GATE_STEP_SECS", 0.4)
    with land_deadline.scope():
        with pytest.raises(land_deadline.LandDeadlineExceeded) as raised:
            with land_byte_gate.admit(repo, anchor_root=repo):
                pytest.fail("the gate was held past its step")
    fields = land_deadline.parked_fields(raised.value)
    assert fields["bound_by"] == land_deadline.BOUND_STEP
    assert fields["left_secs"] is not None and fields["left_secs"] > 0.0


def test_parked_fields_without_a_ledger_still_answer_not_started():
    fields = land_deadline.parked_fields(land_deadline.LandDeadlineExceeded("land_run.git", step=True))
    assert fields["status"] == "not-started" and fields["spent_by"] == "" and fields["left_secs"] is None
    assert "land_run.git" in fields["error"]
