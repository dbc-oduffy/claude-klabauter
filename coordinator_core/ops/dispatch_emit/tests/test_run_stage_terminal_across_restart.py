"""A run staged by one engine process and landed or closed by another still ends at a terminal run-stage.

The restart is modelled as the absence of any in-process state: staging wrote only files, and the later
landing sees the run through its id and the close-job record alone."""

from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import run_close

RUN = "ask-20261103T063704-417c3b"
SHA = "a" * 40


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-1")


def _deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=5)


def _stage(root) -> str:
    return c.read_run_stage(root, RUN)["stage"]


def _staged(root) -> None:
    c.write_run_stage(root, RUN, "staged", steps_planned=1)
    assert _stage(root) == "staged"


def test_write_close_job_after_restart_lands_the_staged_run(tmp_path):
    _staged(tmp_path)
    run_close.write_close_job(tmp_path, f"{RUN}.workflow", [], _deadline(), terminal_sha=SHA, inputs={})
    stamped = c.read_run_stage(tmp_path, RUN)
    assert stamped["stage"] == "landed" and stamped["warp_run"] == RUN
    assert stamped["steps_planned"] == 1


def test_write_close_job_with_incomplete_rows_leaves_the_stage(tmp_path):
    _staged(tmp_path)
    run_close.write_close_job(
        tmp_path, RUN, [], _deadline(), terminal_sha=SHA, inputs={"incomplete": ["C2"]},
    )
    assert _stage(tmp_path) == "staged"


def test_write_close_job_not_met_criterion_or_no_terminal_sha_leaves_the_stage(tmp_path):
    _staged(tmp_path)
    run_close.write_close_job(
        tmp_path, RUN, [], _deadline(), terminal_sha=SHA, criterion={"status": "not_met", "observation": "x"},
    )
    run_close.write_close_job(tmp_path, RUN, [], _deadline())
    assert _stage(tmp_path) == "staged"


def test_amend_close_job_settles_a_run_the_landing_missed(tmp_path):
    run_close.write_close_job(tmp_path, RUN, [], _deadline(), terminal_sha=SHA, inputs={})
    _staged(tmp_path)  # the landing-time stamp never happened
    run_close.amend_close_job(tmp_path, RUN, criterion={"status": "met", "observation": "ok"})
    assert _stage(tmp_path) == "landed"


def test_settle_never_regresses_or_creates(tmp_path):
    assert run_close.settle_run_stage(tmp_path, RUN) is False
    assert c.read_run_stage(tmp_path, RUN) is None
    for stage in ("halted", "landed"):
        c.write_run_stage(tmp_path, RUN, stage)
        assert run_close.settle_run_stage(tmp_path, RUN) is False
        assert _stage(tmp_path) == stage


@pytest.mark.parametrize("bad", [None, "", "../x", 5])
def test_settle_refuses_an_unusable_run_id(tmp_path, bad):
    assert run_close.settle_run_stage(tmp_path, bad) is False


def _already_landed_call(tmp_path, monkeypatch, incomplete):
    from coordinator_core.ops.dispatch_emit import land_run

    monkeypatch.setattr(land_run, "_redeem_or_answer", lambda *a, **k: {land_run.ALREADY_LANDED_KEY: True})
    monkeypatch.setattr(land_run, "_requeue_close", lambda *a, **k: None)
    params = {
        "script_path": f"scripts/{RUN}.workflow.js", "run_id": RUN, "run_token": "tok",
        "incomplete_chunks": incomplete, "landed_chunks": [], "session_id": None,
    }
    return land_run._land_inner(tmp_path, tmp_path, params, {}, {})


def test_land_run_already_landed_answer_after_restart_lands_the_staged_run(tmp_path, monkeypatch):
    _staged(tmp_path)
    out = _already_landed_call(tmp_path, monkeypatch, [])
    assert out.get("already_landed") is True, out
    assert _stage(tmp_path) == "landed"


def test_land_run_already_landed_answer_with_incomplete_chunks_leaves_the_stage(tmp_path, monkeypatch):
    _staged(tmp_path)
    out = _already_landed_call(tmp_path, monkeypatch, ["C2"])
    assert out.get("already_landed") is True, out
    assert _stage(tmp_path) == "staged"
