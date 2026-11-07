"""A row dropped as landed outside the executor is named, with its crediting sha, in the stage reply."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import (
    PLAN_REL,
    RUN,
    _plan,
    _row,
    _stage,
)


def test_outside_credit_is_named_in_the_reply_with_its_sha(tmp_path):
    _plan(tmp_path, _row("FA") + _row("F3"))
    resume_snapshot.write(
        tmp_path, RUN, PLAN_REL, ["FA"], ["F3"], reasons={"FA": "landed outside the executor at 011d651482"}
    )
    reply = _stage(tmp_path, ["F3"])
    assert "error" not in reply
    assert reply["outside_credited"] == [{"id": "FA", "sha": "011d651482"}]


def test_reason_constant_round_trips_through_the_reply(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import OUTSIDE_CREDIT_REASON

    _plan(tmp_path, _row("FA") + _row("F3"))
    reason = OUTSIDE_CREDIT_REASON.format(sha="deadbeef01")
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["FA"], ["F3"], reasons={"FA": reason})
    reply = _stage(tmp_path, ["F3"])
    assert reply["outside_credited"] == [{"id": "FA", "sha": "deadbeef01"}]


def test_other_drop_reasons_are_not_outside_credit(tmp_path):
    _plan(tmp_path, _row("FA") + _row("F3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["FA"], ["F3"], reasons={"FA": "landed at abc1234"})
    reply = _stage(tmp_path, ["F3"])
    assert "error" not in reply
    assert [d["id"] for d in reply["dropped_rows"]] == ["FA"]
    assert "outside_credited" not in reply
