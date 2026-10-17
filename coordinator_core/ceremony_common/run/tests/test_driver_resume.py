"""The run driver after a kill: a step that already has a `ran` row is not dispatched again.

Built on `test_driver.Box`. The kill is a test-local BaseException raised from a dispatched step;
`launch` releases its lock on the way out, so the test puts back the lock a killed process would
have left (same run id, provably dead pid). The envelope and front-door steps run again: later steps read their output from memory.

Run: python -m pytest coordinator_core/ceremony_common/run/tests/test_driver_resume.py -q
"""
from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ceremony_common.run import driver, single_flight
from coordinator_core.ceremony_common.run.tests.test_driver import (
    DAY,
    Box,
    _directive,
    _point,
    _results,
    _step,
)


class _Kill(BaseException):
    """Stands in for the process kill: not an Exception, so the runner's raise arm does not catch it."""


@pytest.fixture
def box(tmp_path, capsys):
    return Box(tmp_path, capsys)


def _kill_on(box: Box, step_id: str) -> dict:
    """Make the box's dispatch table raise `_Kill` on `step_id` while `armed['on']` is true."""
    armed = {"on": True}
    original = box.table

    def table():
        base = original()

        def wrap(inner):
            def call(step, **kw):
                if armed["on"] and step.id == step_id:
                    box.ran.append(step.id)
                    raise _Kill()
                return inner(step, **kw)

            return call

        return {k: (wrap(v) if k in ("cli", "op", "inproc") else v) for k, v in base.items()}

    box.table = table
    return armed


def test_a_relaunch_after_a_killed_launch_does_not_repeat_a_step_that_ran(box, monkeypatch):
    seen = []
    real_acquire = single_flight.acquire

    def spy(*a, **kw):
        adm = real_acquire(*a, **kw)
        seen.append(adm)
        return adm

    monkeypatch.setattr(single_flight, "acquire", spy)
    spec = box.spec(
        front_door=[_step("prep-1", "prep"), _step("orient", "orient")],
        residue=[[_step("kill-1", "decide")]],
        stage_of=lambda _id: "orient",
    )
    box.envelope(directives=[_directive("d-1", None)])
    armed = _kill_on(box, "kill-1")
    with pytest.raises(_Kill):
        box.launch(spec)
    assert box.ran.count("d-1") == 1

    dead = seen[0]
    dead.lock_path.parent.mkdir(parents=True, exist_ok=True)
    dead.lock_path.write_text(
        json.dumps({
            "pid": 0, "start_epoch": int(time.time()) - 60, "session_id": "S-dead",
            "run_id": dead.run_id, "day": DAY, "phase": single_flight.PHASE_LAUNCHING,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(single_flight, "_holder_alive", lambda _holder: False)
    armed["on"] = False
    code, _, _ = box.launch(spec, session="S-second")
    assert code == 0
    assert box.ran.count("d-1") == 1
    assert box.ran.count("orient") == 2 and box.ran.count("prep-1") == 2


def test_a_second_finish_after_a_killed_finish_does_not_repeat_a_step_that_ran(box):
    spec = box.spec(
        finish_steps=lambda decisions, rows, *, run: [_step("tail-1", "tail"), _step("tail-2", "tail")]
    )
    box.with_template().envelope([_point("j-1", ["a", "b"])])
    _, out, _ = box.launch(spec)
    run_id = box.run_id(out)
    armed = _kill_on(box, "tail-2")

    with pytest.raises(_Kill):
        box.finish(spec, run_id, _results("j-1", "a"))
    assert box.ran.count("tail-1") == 1

    armed["on"] = False
    code, _, _ = box.finish(spec, run_id, _results("j-1", "a"))
    assert code == 0
    assert box.ran.count("tail-1") == 1
    assert box.ran.count("tail-2") == 2
