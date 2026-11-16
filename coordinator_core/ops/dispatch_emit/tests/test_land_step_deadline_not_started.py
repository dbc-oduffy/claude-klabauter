"""A blocking step with its own deadline ends a land_run as a parked not-started reply naming the step.

The primitive (`land_deadline.step`) is driven with a fake clock; the reply is driven through the
default `dispatch.land_run` product entry with a step forced to run out before and after the commit.
"""

from __future__ import annotations

import time
from contextlib import ExitStack

import pytest

from coordinator_core import dispatch_abandon, ipc
from coordinator_core.benchmarks import land_run_slow_disk_profile as prof
from coordinator_core.ops.dispatch_emit import land_deadline, land_intent, land_run, run_token
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.warm import dispatch_ack, dispatch_fence

BUDGET = land_deadline.LAND_BUDGET_SECS


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
def cpu(monkeypatch):
    fake = Fake(0.0)
    monkeypatch.setattr(land_deadline, "_cpu", fake)
    return fake


def test_step_yields_the_smaller_of_its_cap_and_the_op_budget(clock):
    with land_deadline.scope():
        clock.now += 4.0
        with land_deadline.step("land_run.x", cap_secs=2.0) as secs:
            assert secs == pytest.approx(2.0)
        with land_deadline.step("land_run.x", cap_secs=60.0) as secs:
            assert secs == pytest.approx(BUDGET - 4.0)
        with land_deadline.step("land_run.x") as secs:
            assert secs == pytest.approx(BUDGET - 4.0)


def test_step_outside_a_scope_is_bounded_by_its_cap_alone(clock):
    with land_deadline.step("land_byte_gate.admit", cap_secs=3.0) as secs:
        assert secs == pytest.approx(3.0)
        assert land_deadline.step_remaining() == pytest.approx(3.0)
    assert land_deadline.step_remaining() is None
    with land_deadline.step("land_byte_gate.admit") as secs:
        assert secs is None


def test_step_check_raises_the_existing_exception_naming_the_step(clock):
    with land_deadline.scope():
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            with land_deadline.step("land_run.reverify", cap_secs=2.0):
                land_deadline.step_check()
                clock.now += 2.5
                assert land_deadline.step_remaining() == 0.0
                land_deadline.step_check()
    assert exc.value.leg == "land_run.reverify" and exc.value.step is True
    assert exc.value.bound_by == land_deadline.BOUND_STEP
    assert isinstance(exc.value, BaseException) and not isinstance(exc.value, Exception)


def test_step_entered_after_the_op_budget_raises_at_once(clock):
    with land_deadline.scope():
        clock.now += BUDGET + 0.1
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            with land_deadline.step("land_run.git"):
                pytest.fail("a step with no time left must not start")
    assert exc.value.leg == "land_run.git" and exc.value.bound_by == land_deadline.BOUND_BUDGET


def test_step_deadline_is_wall_clock_not_thread_cpu(clock, cpu):
    with land_deadline.scope():
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            with land_deadline.step("land_run.flock", cap_secs=1.0):
                clock.now += 1.5
                cpu.now += 0.001
                land_deadline.step_check()


def test_nested_step_is_bounded_by_the_enclosing_one(clock):
    with land_deadline.step("outer", cap_secs=2.0):
        with land_deadline.step("inner", cap_secs=9.0) as secs:
            assert secs == pytest.approx(2.0)
        assert land_deadline.step_remaining() == pytest.approx(2.0)


def test_a_step_that_stays_inside_its_deadline_is_not_refused(clock):
    with land_deadline.scope():
        with land_deadline.step("land_run.git", cap_secs=2.0):
            clock.now += 1.0
            land_deadline.step_check()


def test_parked_fields_name_the_step_and_are_not_an_indeterminate():
    exc = land_deadline.LandDeadlineExceeded("land_run.git", step=True)
    fields = land_deadline.parked_fields(exc)
    assert fields["status"] == "not-started" and fields["parked"] is True
    assert fields["step"] == "land_run.git" and "land_run.git" in fields["error"]


def test_land_budget_plus_reserve_sits_below_the_dispatch_timeout():
    assert BUDGET + land_deadline.RESERVE_SECS < ipc._timeout_for("dispatch.land_run")
    assert land_deadline.WALL_HANG_CEILING_SECS < ipc._timeout_for("dispatch.land_run")


KEY = "321-655"


@pytest.fixture
def landing(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", prof.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    claim_dir = tmp_path / "svc" / "dispatch-claims"
    claim_dir.mkdir(parents=True)
    dispatch_fence.hold(str(claim_dir), KEY)
    assert dispatch_ack.journal_dir_beside(claim_dir)
    with ExitStack() as stack:
        root, base = prof.build_land_fixture(tmp_path / "fx", mode="clean", tracked=5, chain=2, stack=stack)
        try:
            yield root, base
        finally:
            dispatch_fence.release()
            land_intent._held = None


def _step_runs_out_at(monkeypatch, phase: str) -> None:
    """Run the real `mark`; right after `phase`'s mark, a blocking step blows its own deadline."""
    real = land_intent.mark

    def mark(p, sha=None, **kw):
        real(p, sha, **kw)
        if p == phase:
            with land_deadline.step("land_run.blocked_step", cap_secs=0.05):
                time.sleep(0.1)
                land_deadline.step_check()

    monkeypatch.setattr(land_intent, "mark", mark)


def _fire(root, base) -> dict:
    token = run_token.mint(root, prof.RUN_ID, prof.SCRIPT, session_id=prof.SELF)
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": prof.SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": prof.SELF, "run_id": prof.RUN_ID, "run_token": token},
        repo_root=root / ".git",
    )


def _head(root) -> str:
    return prof._git(["rev-parse", "HEAD"], root).strip()


def test_step_breach_before_the_commit_replies_parked_not_started_inside_the_budget(landing, monkeypatch):
    root, base = landing
    before = _head(root)
    _step_runs_out_at(monkeypatch, land_intent.PHASE_COMMITTING)
    t0 = time.monotonic()
    reply = _fire(root, base)
    assert time.monotonic() - t0 < ipc._timeout_for("dispatch.land_run")
    assert _head(root) == before
    assert reply["status"] == "not-started" and reply["parked"] is True
    assert reply["step"] == "land_run.blocked_step" and "land_run.blocked_step" in reply["error"]
    assert reply["committed"] is False and reply["sha"] is None
    assert reply["refused"] == land_deadline.REFUSED


def test_step_breach_after_the_commit_reports_committed_never_not_started(landing, monkeypatch):
    root, base = landing
    before = _head(root)
    _step_runs_out_at(monkeypatch, land_intent.PHASE_COMMITTED)
    reply = _fire(root, base)
    assert _head(root) != before
    assert reply["committed"] is True and reply["sha"] == _head(root)
    assert "status" not in reply and "parked" not in reply


def test_a_non_step_deadline_keeps_the_plain_halt_reply(landing, monkeypatch):
    root, base = landing
    real = land_intent.mark

    def mark(p, sha=None, **kw):
        real(p, sha, **kw)
        if p == land_intent.PHASE_COMMITTING:
            raise land_deadline.LandDeadlineExceeded("land_run.test_leg", [])

    monkeypatch.setattr(land_intent, "mark", mark)
    reply = _fire(root, base)
    assert reply["refused"] == land_deadline.REFUSED
    assert "status" not in reply and "parked" not in reply


def test_dispatcher_clamp_stays_below_the_cap_for_a_step(clock, monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 3.0 + dispatch_abandon.REPLY_MARGIN_SECS)
    with land_deadline.scope():
        with land_deadline.step("land_run.git", cap_secs=20.0) as secs:
            assert secs == pytest.approx(3.0)
        clock.now += 3.1
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            with land_deadline.step("land_run.git"):
                pass
    assert exc.value.bound_by == land_deadline.BOUND_DISPATCHER
