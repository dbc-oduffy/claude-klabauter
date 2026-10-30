"""An xs-s-run close never leaves its ``review`` or ``review-stamp`` leg ``queued`` with nothing to run it.

Drives ``dispatch.land_run`` on a minted XS spine (``derived_from: sizing object``) and pays the
close job in-process, as the detached ``close_background`` job does.

Root cause found at base: ``land_run`` reports ``review`` as ``queued`` unconditionally, but the close
job record gains a ``review`` entry only when the ``review-stamp`` leg mints a stamp
(``close_background._queue_review``). When the stamp leg settles ``not-applicable`` (skipped), the
reply keeps naming ``review`` queued while no record entry exists for any job to pay or time out, so
the leg reads queued forever and never reaches a recorded outcome.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import land_run, run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    PLAN,
    RUN,
    _git,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REVIEW_LEGS = ("review", "review-stamp")


def _make_xs_spine(root) -> None:
    path = root / PLAN
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("---\ntitle: p\n", "---\ntitle: p\nderived_from: sizing object\n", 1), encoding="utf-8")


def _pay_job_in_process(monkeypatch) -> list:
    """Replace the detached job launch with the job's own legs run now; returns the outcomes it recorded.
    ``post-close`` and ``orientation-refresh`` are inert so no index rebuild or scip launcher fires."""
    paid: list = []
    inert = LegResult("not-applicable", "inert in test")
    runners = {**cb.DEFAULT_RUNNERS, "post-close": lambda _c: inert, "orientation-refresh": lambda _c: inert}

    def launch(root, key, batons, memo) -> bool:
        ctx = cb.JobContext(root, root / ".git", key, tuple(batons), memo)
        paid.append(cb.run_and_continue(ctx, launch=lambda _c: False, runners=runners))
        return True

    monkeypatch.setattr(land_run, "_launch_close_job", launch)
    return paid


def test_xs_close_review_legs_reach_a_recorded_non_queued_outcome(repo, monkeypatch):
    _make_xs_spine(repo)
    paid = _pay_job_in_process(monkeypatch)

    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert out["close"]["kind"] == "xs-s-run", out["close"]
    assert paid, "the close job was never paid"
    record = run_close.read_close_job(repo, RUN, datetime.now().astimezone())
    left = {
        leg: (record[leg].status if leg in record else "absent from the close job record")
        for leg in REVIEW_LEGS
        if leg not in record or record[leg].status == "queued" or not record[leg].detail
    }
    assert not left, {"review legs queued and unrun": left, "reply": out["close"]["legs"]}


def test_a_leg_the_reply_reports_queued_is_owed_by_the_close_job_record(repo, monkeypatch):
    _make_xs_spine(repo)
    _pay_job_in_process(monkeypatch)

    out = _land(repo, hand_commit=False)

    record = run_close._load(repo, RUN)["legs"]
    unowed = sorted(
        leg for leg, entry in out["close"]["legs"].items() if entry["status"] == "queued" and leg not in record
    )
    assert not unowed, {"reported queued, no record entry": unowed}


def test_a_review_leg_settled_for_a_skipped_stamp_names_the_stamp_reason(repo, monkeypatch):
    _make_xs_spine(repo)
    _pay_job_in_process(monkeypatch)

    _land(repo, hand_commit=False)

    legs = run_close._load(repo, RUN)["legs"]
    assert legs["review-stamp"]["status"] == "failed", legs["review-stamp"]
    assert legs["review"]["status"] == "not-applicable", legs["review"]
    assert "review did not run" in legs["review"]["detail"], legs["review"]
    assert "carries no plan_id" in legs["review"]["detail"], legs["review"]


def test_every_leg_the_job_owed_is_settled_once_the_job_has_run(repo, monkeypatch):
    _make_xs_spine(repo)
    _pay_job_in_process(monkeypatch)

    _land(repo, hand_commit=False)

    record = run_close._load(repo, RUN)["legs"]
    assert record and {leg: e["status"] for leg, e in record.items() if e["status"] == "queued"} == {}
