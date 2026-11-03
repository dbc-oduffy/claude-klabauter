"""review_prep_legs: attach across the to_thread hop, floor, and child-CPU attribution."""

from __future__ import annotations

import asyncio
import time

from coordinator_core import ipc
from coordinator_core.ops import review_prep_legs
from coordinator_core.telemetry import op_row_fields


def _spin(ms: float) -> None:
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        pass


def _dispatch(fn):
    """Mimic ipc.dispatch_message: bind both vars before the hop, drain after."""

    async def go():
        ot = op_row_fields.begin()
        cpu: list = []
        ct = ipc._op_cpu_secs_var.set(cpu)
        try:
            await asyncio.to_thread(review_prep_legs.run, fn)
        finally:
            ipc._op_cpu_secs_var.reset(ct)
        return op_row_fields.drain(ot)

    return asyncio.run(go())


def test_legs_registry_names_every_phase():
    assert len(review_prep_legs.LEGS) == len(set(review_prep_legs.LEGS)) == 13
    assert "freeze_whole" in review_prep_legs.LEGS


def test_every_leg_name_used_by_review_prep_is_registered():
    import re
    from pathlib import Path

    from coordinator_core.ops import review_prep

    src = Path(review_prep.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r'review_prep_legs\.leg\("([^"]+)"\)', src))
    assert used == set(review_prep_legs.LEGS)


def test_attach_inside_to_thread_is_drained_by_caller():
    def body():
        with review_prep_legs.leg("freeze_whole"):
            _spin(120)
        return 7

    fields = _dispatch(body)
    legs = fields["legs"]
    assert legs[0]["name"] == "freeze_whole"
    assert legs[0]["ms"] >= 50.0
    assert legs[0]["child_ms"] == 0.0


def test_no_legs_key_under_floor():
    def body():
        with review_prep_legs.leg("partition"):
            pass

    assert "legs" not in _dispatch(body)


def test_child_cpu_only_leg_is_attributed_and_ranked():
    def body():
        with review_prep_legs.leg("freeze_whole"):
            _spin(80)
        with review_prep_legs.leg("foreign_claims"):
            ipc.charge_op_cpu_secs(0.5)

    legs = _dispatch(body)["legs"]
    assert [r["name"] for r in legs] == ["foreign_claims", "freeze_whole"]
    assert legs[0]["child_ms"] == 500.0
    assert legs[0]["ms"] < 50.0


def test_top_three_only():
    def body():
        for i, name in enumerate(review_prep_legs.LEGS[:5]):
            with review_prep_legs.leg(name):
                ipc.charge_op_cpu_secs(0.1 * (i + 1))

    legs = _dispatch(body)["legs"]
    assert len(legs) == 3
    assert legs[0]["name"] == review_prep_legs.LEGS[4]


def test_leg_outside_run_is_noop_and_run_returns_value_and_resets():
    with review_prep_legs.leg("freeze_whole"):
        pass
    assert review_prep_legs.run(lambda: 5) == 5
    assert review_prep_legs._child_secs.get() is None


def test_exception_still_attaches_and_resets():
    ot = op_row_fields.begin()

    def body():
        with review_prep_legs.leg("hash_contents"):
            _spin(100)
        raise ValueError("x")

    try:
        review_prep_legs.run(body)
    except ValueError:
        pass
    fields = op_row_fields.drain(ot)
    assert fields["legs"][0]["name"] == "hash_contents"
    assert review_prep_legs._child_secs.get() is None
