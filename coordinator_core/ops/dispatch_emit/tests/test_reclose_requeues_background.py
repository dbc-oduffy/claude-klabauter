"""A re-close requeues budget-failed background legs past their deadline and launches the job."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import close_without_script as cws, run_close

RUN = "run-q.workflow"
FAILED = ("sizing-ship", "plan-reconciliation", "baton-drain", "post-close")


def _legs(root):
    return json.loads((root / run_close.close_job_path(RUN)).read_text(encoding="utf-8"))


def _stale(root):
    run_close.write_close_job(root, RUN, [*FAILED, "review-stamp"], datetime.now().astimezone() - timedelta(minutes=5))
    for leg in FAILED:
        run_close.record_leg_outcome(root, RUN, leg, "failed", "not started: job process time 469ms reached 400ms")
    run_close.record_leg_outcome(root, RUN, "review-stamp", "ran", "done")


def test_failed_background_legs_are_requeued_with_a_fresh_deadline_and_launched(tmp_path, monkeypatch):
    _stale(tmp_path)
    launched = []
    monkeypatch.setattr(cb, "_launch_continuation", lambda ctx: launched.append(ctx.run_id) or True)

    cws._requeue_background(tmp_path, tmp_path, RUN)

    rec = _legs(tmp_path)
    assert launched == [RUN]
    assert datetime.fromisoformat(rec["deadline"]) > datetime.now().astimezone()
    for leg in FAILED:
        assert rec["legs"][leg] == {"status": "queued", "detail": "requeued by re-close"}
    assert rec["legs"]["review-stamp"]["status"] == "ran"


def test_nothing_owed_launches_nothing(tmp_path, monkeypatch):
    run_close.write_close_job(tmp_path, RUN, ["post-close"], datetime.now().astimezone() + timedelta(minutes=5))
    run_close.record_leg_outcome(tmp_path, RUN, "post-close", "ran", "ok")
    monkeypatch.setattr(cb, "_launch_continuation", lambda ctx: (_ for _ in ()).throw(AssertionError("launched")))
    cws._requeue_background(tmp_path, tmp_path, RUN)
